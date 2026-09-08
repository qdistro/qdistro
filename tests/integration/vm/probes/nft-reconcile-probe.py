#!/usr/bin/env python3
"""Exercise the production nft repair against a real, isolated Linux network stack.

Runs in a fresh user+network namespace; no host/VM firewall is modified.
libnftables provides the nft command boundary even on hosts without its CLI.
The exact _nft_ensure_table method is compiled from the supplied production
source; all subprocesses, rules, transactions and packets are real.
"""
import ast
import ctypes
import ctypes.util
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace


def nft(command):
    lib = ctypes.CDLL(ctypes.util.find_library("nftables"))
    lib.nft_ctx_new.argtypes = [ctypes.c_uint]
    lib.nft_ctx_new.restype = ctypes.c_void_p
    for name in ("nft_ctx_buffer_output", "nft_ctx_buffer_error", "nft_ctx_free"):
        getattr(lib, name).argtypes = [ctypes.c_void_p]
    for name in ("nft_ctx_get_output_buffer", "nft_ctx_get_error_buffer"):
        getattr(lib, name).argtypes = [ctypes.c_void_p]
        getattr(lib, name).restype = ctypes.c_char_p
    lib.nft_run_cmd_from_buffer.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.nft_run_cmd_from_buffer.restype = ctypes.c_int
    context = lib.nft_ctx_new(0)
    lib.nft_ctx_buffer_output(context)
    lib.nft_ctx_buffer_error(context)
    try:
        rc = lib.nft_run_cmd_from_buffer(context, command.encode())
        out = (lib.nft_ctx_get_output_buffer(context) or b"").decode()
        err = (lib.nft_ctx_get_error_buffer(context) or b"").decode()
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)
    finally:
        lib.nft_ctx_free(context)


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def rules(text):
    result = nft(text)
    assert result.returncode == 0, result.stderr
    return result.stdout


def pass_test(label):
    print(f"PASS: {label}", flush=True)


def listener(port):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen()

    def accept():
        while True:
            connection, _ = server.accept()
            with connection:
                connection.sendall(b"ok")
    threading.Thread(target=accept, daemon=True).start()
    return server


def run_probe(source, parent_net):
    assert os.readlink("/proc/self/ns/net") != parent_net, "must run in a fresh network namespace"
    tree = ast.parse(Path(source).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_SystemOps")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_nft_ensure_table")
    scope = {"subprocess": subprocess}
    exec(compile(ast.Module(body=[method], type_ignores=[]), source, "exec"), scope)
    ops = SimpleNamespace(_NFT_TABLE="qdistro_egress")
    ensure = lambda: scope["_nft_ensure_table"](ops)
    command("ip", "link", "set", "lo", "up")
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1\n")
    peers = {}
    listeners = []
    with tempfile.TemporaryDirectory(prefix="qd-nft-kernel-") as work:
        wrapper = Path(work) / "nft"
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} --nft \"$@\"\n")
        wrapper.chmod(0o700)
        os.environ["PATH"] = work + os.pathsep + os.environ["PATH"]
        try:
            ensure()
            pass_test("production batch accepted by kernel")
            rules("add element inet qdistro_egress blocked_uids { 4242 }\n"
                  "add element inet qdistro_egress nat_subnets { 10.128.1.0/30, 10.128.2.0/30 }\n")
            ensure()
            table = rules("list table inet qdistro_egress")
            assert "4242" in table and "10.128.1.0/30" in table and "10.128.2.0/30" in table, table
            pass_test("repeated reconcile preserves both dynamic sets")
            for name, router, address, prefix in (
                ("siloa", "10.128.1.1", "10.128.1.2", "30"),
                ("silob", "10.128.2.1", "10.128.2.2", "30"),
                ("lan", "192.168.1.1", "192.168.1.2", "24"),
                ("public", "198.51.100.1", "198.51.100.2", "24"),
            ):
                peer = subprocess.Popen(["unshare", "--net", sys.executable, str(Path(__file__).resolve()), "--peer"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                peers[name] = peer
                assert peer.stdout.readline().strip() == "ready", f"peer {name} failed to start"
                command("ip", "link", "add", name, "type", "veth", "peer", "name", "p" + name)
                command("ip", "link", "set", "p" + name, "netns", str(peer.pid))
                command("ip", "addr", "add", router + "/" + prefix, "dev", name)
                command("ip", "link", "set", name, "up")
                base = ["nsenter", "-t", str(peer.pid), "-n", "ip"]
                command(*base, "link", "set", "lo", "up")
                command(*base, "addr", "add", address + "/" + prefix, "dev", "p" + name)
                command(*base, "link", "set", "p" + name, "up")
                command(*base, "route", "add", "default", "via", router)
            # Additional LAN management ranges exercise every bogon selector,
            # not just RFC1918 192.168/16.
            for router, address in (("172.16.1.1", "172.16.1.2"),
                                    ("169.254.1.1", "169.254.1.2"),
                                    ("100.64.1.1", "100.64.1.2")):
                command("ip", "addr", "add", router + "/24", "dev", "lan")
                command("nsenter", "-t", str(peers["lan"].pid), "-n", "ip", "addr",
                        "add", address + "/24", "dev", "plan")
            listeners = [listener(23456), listener(53)]

            def reaches(peer, destination, port=23456):
                argv = [] if peer is None else ["nsenter", "-t", str(peers[peer].pid), "-n"]
                argv += [sys.executable, "-c", "import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),0.5); assert s.recv(2)==b'ok'", destination, str(port)]
                result = subprocess.run(argv, capture_output=True, text=True)
                return result.returncode == 0

            def packet_matrix():
                # Positive controls establish that routes and listeners work,
                # so failed probes cannot pass merely because peers are dead.
                assert reaches("lan", "192.168.1.1"), "host listener positive control failed"
                assert reaches(None, "10.128.2.2"), "sibling listener positive control failed"
                assert reaches(None, "192.168.1.2"), "LAN listener positive control failed"
                assert reaches("siloa", "10.128.1.1", 53), "silo resolver exception failed"
                assert reaches("siloa", "198.51.100.2"), "public forwarding/NAT failed"
                assert not reaches("siloa", "10.128.1.1"), "silo reached host service"
                assert not reaches("siloa", "10.128.2.2"), "silo reached sibling silo"
                for destination in ("192.168.1.2", "172.16.1.2", "169.254.1.2", "100.64.1.2"):
                    assert reaches(None, destination), f"LAN/bogon positive control failed: {destination}"
                    assert not reaches("siloa", destination), f"silo reached LAN/bogon: {destination}"
                assert not reaches("lan", "10.128.1.2"), "LAN initiated into silo"
                assert not reaches("public", "10.128.1.2"), "public peer initiated into silo"

            packet_matrix()
            pass_test("packets: DNS/public allowed; host/sibling/LAN and unsolicited silo ingress denied")
            rules("flush chain inet qdistro_egress in\nflush chain inet qdistro_egress forward\n")
            assert reaches("siloa", "10.128.1.1"), "damage control did not open host path"
            assert reaches("siloa", "10.128.2.2"), "damage control did not open sibling path"
            ensure()
            assert rules("list table inet qdistro_egress") == table, "canonical repair changed sets/rules"
            packet_matrix()
            pass_test("damaged-chain repair restores packet isolation and preserves dynamic sets")
            before = rules("list table inet qdistro_egress")
            os.environ["QDISTRO_NFT_TEST_INVALID"] = "1"
            try:
                ensure()
            except RuntimeError:
                pass
            else:
                raise AssertionError("invalid repair was accepted")
            finally:
                del os.environ["QDISTRO_NFT_TEST_INVALID"]
            assert rules("list table inet qdistro_egress") == before, "failed transaction altered existing firewall"
            packet_matrix()
            pass_test("invalid transaction rolls back without losing prior packet protection")
            rules("add element inet qdistro_egress blocked_uids { 0 }")
            assert not reaches(None, "198.51.100.2"), "blocked UID reached public peer"
            rules("delete element inet qdistro_egress blocked_uids { 0 }")
            assert reaches(None, "198.51.100.2"), "UID backstop positive control failed"
            pass_test("init-namespace UID backstop drops real traffic")
        finally:
            for peer in peers.values():
                peer.terminate()
            for peer in peers.values():
                peer.wait(timeout=5)
            for server in listeners:
                server.close()


def main():
    if sys.argv[1] == "--nft":
        assert sys.argv[2:] == ["-f", "-"]
        batch = sys.stdin.read()
        if os.environ.get("QDISTRO_NFT_TEST_INVALID"):
            batch += "add rule inet qdistro_egress nonexistent_chain drop\n"
        result = nft(batch)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return 0 if result.returncode == 0 else 1
    if sys.argv[1] == "--peer":
        server = listener(23456)
        print("ready", flush=True)
        sys.stdin.read()
        server.close()
        return 0
    if sys.argv[1] == "--inside":
        run_probe(sys.argv[2], sys.argv[3])
        return 0
    return subprocess.run(["unshare", "--user", "--map-root-user", "--net", sys.executable,
                           str(Path(__file__).resolve()), "--inside", str(Path(sys.argv[1]).resolve()),
                           os.readlink("/proc/self/ns/net")]).returncode


if __name__ == "__main__":
    sys.exit(main())
