"""Test PKI + loopback TLS server for the pin-proxy tests.

Not a test module (no ``test_`` prefix): imported by ``test_pin_proxy.py``
and by ``pinproxy_chromium_harness.py``, which runs in a subprocess.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

MARKER = b"PINPROXY-SERVED"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject_cn, key, issuer_cn, issuer_key, *, ca, san=None):
    now = datetime.now(UTC)
    b = (x509.CertificateBuilder()
         .subject_name(_name(subject_cn))
         .issuer_name(_name(issuer_cn))
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - timedelta(days=1))
         .not_valid_after(now + timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None),
                        critical=True))
    if ca:
        b = b.add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False,
            key_encipherment=False, data_encipherment=False,
            key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
        b = b.add_extension(x509.SubjectKeyIdentifier.from_public_key(
            key.public_key()), critical=False)
    else:
        b = b.add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(n) for n in san]), critical=False)
        b = b.add_extension(x509.ExtendedKeyUsage(
            [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
        issuer_key.public_key()), critical=False)
    return b.sign(issuer_key, hashes.SHA256())


def spki_pin(cert: x509.Certificate) -> str:
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()


def pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


class Pki:
    """Two CAs (``good``, ``rogue``), each with a leaf for ``host``."""

    def __init__(self, directory: str, host: str = "pinned.test",
                 extra_names=("pinned.localhost",)):
        self.dir = directory
        self.host = host
        self.good_ca_key = ec.generate_private_key(ec.SECP256R1())
        self.good_ca = _cert("Good CA", self.good_ca_key, "Good CA",
                             self.good_ca_key, ca=True)
        self.rogue_ca_key = ec.generate_private_key(ec.SECP256R1())
        self.rogue_ca = _cert("Rogue CA", self.rogue_ca_key, "Rogue CA",
                              self.rogue_ca_key, ca=True)
        self.good_key = ec.generate_private_key(ec.SECP256R1())
        self.good_leaf = _cert(host, self.good_key, "Good CA",
                               self.good_ca_key, ca=False, san=[host, *extra_names])
        self.rogue_key = ec.generate_private_key(ec.SECP256R1())
        self.rogue_leaf = _cert(host, self.rogue_key, "Rogue CA",
                                self.rogue_ca_key, ca=False,
                                san=[host, *extra_names])
        self.trust_file = os.path.join(directory, "trust.pem")
        with open(self.trust_file, "wb") as f:
            f.write(pem(self.good_ca) + pem(self.rogue_ca))

    def client_context(self) -> ssl.SSLContext:
        """Trusts both CAs: the rogue chain is 'CA-valid', as with a
        mis-issued certificate or a TLS-inspecting middlebox."""
        ctx = ssl.create_default_context(cafile=self.trust_file)
        return ctx

    def server_files(self, which: str, extra=()) -> tuple[str, str]:
        leaf, key = ((self.good_leaf, self.good_key) if which == "good"
                     else (self.rogue_leaf, self.rogue_key))
        chain = os.path.join(self.dir, f"{which}-chain.pem")
        keyf = os.path.join(self.dir, f"{which}-key.pem")
        with open(chain, "wb") as f:
            f.write(pem(leaf) + b"".join(pem(c) for c in extra))
        with open(keyf, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        return chain, keyf


class TlsServer:
    """Loopback HTTPS-ish server: answers every request with MARKER."""

    def __init__(self, chain: str, key: str):
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(chain, key)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.served = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(raw,), daemon=True).start()

    def _one(self, raw):
        try:
            raw.settimeout(10)
            with self.ctx.wrap_socket(raw, server_side=True) as tls:
                data = tls.recv(65536)
                if not data:
                    return
                self.served += 1
                body = b"<html><body>" + MARKER + b"</body></html>"
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                            b"Content-Length: " + str(len(body)).encode()
                            + b"\r\nConnection: close\r\n\r\n" + body)
        except (OSError, ssl.SSLError):
            pass

    def close(self):
        self.sock.close()


def loopback_resolver(host_map: dict):
    """getaddrinfo stand-in: names in ``host_map`` resolve to 127.0.0.1."""
    def resolve(host, port, type=socket.SOCK_STREAM):
        if host not in host_map:
            raise socket.gaierror(f"unknown host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 ("127.0.0.1", host_map[host]))]
    return resolve
