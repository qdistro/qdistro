"""Certificate pin *store* for qdbrowser, plus an error-path hook.

WHAT THIS MODULE DOES (read this before calling anything "pinning"):

  - Loads an admin pin map and evaluates a DER chain against it
    (``PinStore.evaluate``). That evaluation is real and unit-tested.
  - Hooks ``QWebEnginePage.certificateError`` on every page the browser
    creates (``install_cert_policy_on_page``, called from
    ``webview.WebView.__init__``). When Chromium has ALREADY rejected a
    chain for a pinned host and the chain does not carry a pinned SPKI,
    the hook calls ``rejectCertificate()`` so the load is hard-denied
    and the journal records it.

WHAT IT DOES NOT DO — KNOWN LIMITATION (iso2 `13` E2):

  ``certificateError`` fires only for chains the system trust store has
  already refused. A pinned host presenting a CA-valid certificate with
  the *wrong* key (public MITM CA, enterprise TLS interception, a
  mis-issued cert) never raises the signal, so ``PinStore.evaluate`` is
  never consulted and the connection succeeds. QtWebEngine 6.x exposes
  the peer chain ONLY via ``QWebEngineCertificateError`` (error path);
  ``QWebEngineUrlRequestInterceptor``/``QWebEngineUrlRequestInfo``,
  ``QWebEngineLoadingInfo`` and ``QWebEngineProfile`` carry no
  certificate. Until an API exposes the chain on successful handshakes
  this is NOT HPKP / SPKI pinning and must not be described as such.
  Tracked in qdistro ``todo/iso2/13-qdbrowser.md`` E2.

Historical defect: this module used to connect the signal on a
``QWebEngineProfile``. No Qt release has ever had
``QWebEngineProfile.certificateError`` (it is a ``QWebEnginePage``
signal), so the hook was never installed at all. ``install_cert_policy``
now only registers the active store; pages are wired individually.

Qt 6 decision semantics (verified against qtwebengine 6.11
``WebContentsDelegateQt::allowCertificateError``): after the signal
returns, an error that was neither ``acceptCertificate()``-ed,
``rejectCertificate()``-ed nor ``defer()``-ed is rejected. There is no
built-in override UI in QWebEnginePage/QWebEngineView. So a certificate
error on ANY host — pinned or not — aborts the load unless some handler
explicitly accepts it; qdbrowser never calls ``acceptCertificate()``.

On-disk files (both optional; a missing file means an empty map):

  - ``/etc/qdistro/cert-pins.json`` — ``{"hostname": ["sha256/<b64-spki>",
    ...]}``; the pin string format is Chromium's HPKP wire format,
    ``sha256/<base64(SHA256(SubjectPublicKeyInfo DER))>``.
  - ``/etc/qdistro/cert-overrides.json`` — break-glass list of hostnames
    that skip pin evaluation entirely (``["host1", ...]``).
  - ``~/.config/qdbrowser/cert-pins.json`` — user-scope entries merged on
    top of the system file (per-host override), for dev iteration.

Exports:

  - ``load_pin_store(...)`` — returns a ``PinStore``.
  - ``PinStore.is_overridden`` / ``pins_for`` / ``is_pinned`` /
    ``evaluate(host, der_certs)``.
  - ``install_cert_policy(profile, store)`` — registers ``store`` as the
    active store (profile-level bookkeeping only; see above).
  - ``install_cert_policy_on_page(page, store=None)`` — connects the
    error-path hook on one ``QWebEnginePage``.

Journal lines (logger ``qdbrowser.cert``): every evaluation miss logs
``qdbrowser.cert pin_violation host=<h> reason=<r>``; every hard reject
logs ``qdbrowser.cert reject host=<h> reason=<r>``.
"""


from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from collections.abc import Iterable

log = logging.getLogger("qdbrowser.cert")


PIN_PREFIX = "sha256/"


class PinDecision:
    """Result of evaluating a chain against a pin set."""

    OK = "ok"                  # not pinned, or chain matched a pin
    PIN_MISMATCH = "pin_mismatch"
    NO_CERTS = "no_certs"
    OVERRIDDEN = "overridden"  # host in cert-overrides.json

    def __init__(self, kind: str, host: str, detail: str = ""):
        self.kind = kind
        self.host = host
        self.detail = detail

    @property
    def allow(self) -> bool:
        return self.kind in (self.OK, self.OVERRIDDEN)

    def __repr__(self) -> str:
        return (f"PinDecision(kind={self.kind!r}, host={self.host!r}, "
                f"detail={self.detail!r})")


class PinStore:
    """In-memory pin map + override set."""

    def __init__(self,
                 pins: dict | None = None,
                 overrides: Iterable[str] | None = None):
        # Normalize: lowercase host keys, strip any non-canonical pins.
        self._pins: dict = {}
        for host, raw in (pins or {}).items():
            cleaned = [p for p in (raw or [])
                       if isinstance(p, str) and p.startswith(PIN_PREFIX)]
            if cleaned:
                self._pins[host.lower()] = cleaned
        self._overrides: set = {h.lower() for h in (overrides or [])
                                if isinstance(h, str)}

    @property
    def pinned_hosts(self) -> list:
        return sorted(self._pins.keys())

    @property
    def overridden_hosts(self) -> list:
        return sorted(self._overrides)

    def pins_for(self, host: str) -> list:
        return list(self._pins.get((host or "").lower(), []))

    def is_pinned(self, host: str) -> bool:
        return (host or "").lower() in self._pins

    def is_overridden(self, host: str) -> bool:
        return (host or "").lower() in self._overrides

    def evaluate(self, host: str, der_certs: Iterable[bytes]) -> PinDecision:
        """Decide whether to accept a TLS chain for ``host``.

        ``der_certs`` is an iterable of DER-encoded certificate bytes
        (leaf first, then intermediates). For pinning, *any* cert in
        the chain whose SPKI hash matches a pin is sufficient — this
        mirrors Chromium's behaviour and lets pins survive a leaf-key
        rotation when the intermediate is pinned.
        """
        if self.is_overridden(host):
            log.warning("cert override active host=%s", host)
            return PinDecision(PinDecision.OVERRIDDEN, host,
                               "host in cert-overrides.json")
        pins = self.pins_for(host)
        if not pins:
            return PinDecision(PinDecision.OK, host, "host not pinned")
        chain = list(der_certs or [])
        if not chain:
            log.warning("pin check no certs host=%s", host)
            return PinDecision(PinDecision.NO_CERTS, host,
                               "no certificates presented")
        chain_hashes = [spki_hash_from_der(c) for c in chain]
        chain_hashes = [h for h in chain_hashes if h]
        for got in chain_hashes:
            if got in pins:
                return PinDecision(PinDecision.OK, host,
                                   f"matched pin {got}")
        log.warning(
            "qdbrowser.cert pin_violation host=%s reason=pin_mismatch "
            "expected=%s got=%s",
            host, ",".join(pins), ",".join(chain_hashes))
        return PinDecision(PinDecision.PIN_MISMATCH, host,
                           f"none of {chain_hashes} in {pins}")


def spki_hash_from_der(der: bytes) -> str | None:
    """Return the ``sha256/<b64>`` pin string for a DER certificate.

    The SPKI is extracted with a minimal-dependency DER walk:
    Certificate ::= SEQUENCE { tbsCertificate, ... }
    tbsCertificate ::= SEQUENCE {
        [0] version, serialNumber, signature, issuer, validity,
        subject, subjectPublicKeyInfo, ... }
    We walk fields by structure rather than parsing the whole thing —
    a full ASN.1 library would be overkill for a single SPKI extract.

    Returns ``None`` on parse failure; the caller treats that as a
    pin miss (and the journal already logged ``cert_parse_error`` from
    the QWebEngine error signal).
    """
    if not der:
        return None
    try:
        spki = _extract_spki(der)
    except Exception as exc:
        log.warning("spki extract failed: %s", exc)
        return None
    if spki is None:
        return None
    digest = hashlib.sha256(spki).digest()
    return PIN_PREFIX + base64.b64encode(digest).decode("ascii")


def _read_len(buf: bytes, off: int):
    """Read a DER length starting at ``off``. Returns ``(length, new_off)``."""
    first = buf[off]
    off += 1
    if first < 0x80:
        return first, off
    n = first & 0x7F
    if n == 0 or off + n > len(buf):
        raise ValueError("bad DER length")
    length = int.from_bytes(buf[off:off + n], "big")
    return length, off + n


def _read_tlv(buf: bytes, off: int):
    """Return ``(tag, value_bytes, next_off)`` for one DER TLV at ``off``."""
    tag = buf[off]
    length, off = _read_len(buf, off + 1)
    end = off + length
    return tag, buf[off:end], end


def _extract_spki(der: bytes) -> bytes | None:
    """Walk a DER Certificate and return the SubjectPublicKeyInfo bytes
    (the full ``SEQUENCE`` TLV, including outer tag/length — that is
    what HPKP hashes)."""
    # Outer Certificate SEQUENCE
    if not der or der[0] != 0x30:
        return None
    _, tbs_and_more, _ = _read_tlv(der, 0)
    # tbs_and_more is the SEQUENCE contents: tbsCertificate (SEQUENCE),
    # signatureAlgorithm, signatureValue.
    if not tbs_and_more or tbs_and_more[0] != 0x30:
        return None
    _, tbs_inner, _ = _read_tlv(tbs_and_more, 0)
    # tbsCertificate inner sequence:
    #   [0] version (optional, EXPLICIT)
    #   serialNumber, signature, issuer, validity, subject,
    #   subjectPublicKeyInfo, ...
    off = 0
    # Skip optional [0] version
    if off < len(tbs_inner) and tbs_inner[off] == 0xA0:
        _, _, off = _read_tlv(tbs_inner, off)
    # serialNumber
    _, _, off = _read_tlv(tbs_inner, off)
    # signature (AlgorithmIdentifier SEQUENCE)
    _, _, off = _read_tlv(tbs_inner, off)
    # issuer (Name)
    _, _, off = _read_tlv(tbs_inner, off)
    # validity (SEQUENCE)
    _, _, off = _read_tlv(tbs_inner, off)
    # subject (Name)
    _, _, off = _read_tlv(tbs_inner, off)
    # subjectPublicKeyInfo (SEQUENCE) — capture the FULL TLV
    if off >= len(tbs_inner) or tbs_inner[off] != 0x30:
        return None
    start = off
    _, _, off = _read_tlv(tbs_inner, off)
    return bytes(tbs_inner[start:off])


def load_pin_store(system_path: str | None = None,
                   user_path: str | None = None,
                   overrides_path: str | None = None) -> PinStore:
    """Load the pin store. Missing files are treated as empty maps —
    qdbrowser must never refuse to start because the admin file isn't
    there yet.
    """
    pins: dict = {}
    if system_path and os.path.exists(system_path):
        pins.update(_load_json_dict(system_path))
    if user_path and os.path.exists(user_path):
        # User entries override system entries for the same host.
        pins.update(_load_json_dict(user_path))
    overrides: list = []
    if overrides_path and os.path.exists(overrides_path):
        ov = _load_json(overrides_path)
        if isinstance(ov, list):
            overrides = [str(x) for x in ov if isinstance(x, str)]
        elif isinstance(ov, dict) and "hosts" in ov:
            overrides = [str(x) for x in ov["hosts"] if isinstance(x, str)]
    return PinStore(pins=pins, overrides=overrides)


def _load_json(path: str):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception as exc:
        log.warning("cert-pins file %s unreadable: %s", path, exc)
        return None


def _load_json_dict(path: str) -> dict:
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


# The store the page-level hook consults. Set by the window once pins are
# loaded; a page created before that is still covered because the handler
# resolves the store when the error fires.
_ACTIVE_STORE: PinStore | None = None


def set_active_pin_store(store: PinStore | None) -> None:
    global _ACTIVE_STORE
    _ACTIVE_STORE = store


def active_pin_store() -> PinStore | None:
    return _ACTIVE_STORE


def install_cert_policy(profile, store: PinStore) -> None:
    """Register ``store`` as the active pin store. Profile-level only.

    ``certificateError`` is a ``QWebEnginePage`` signal; no Qt release has
    ever exposed it on ``QWebEngineProfile`` (iso2 `13` E2). This function
    therefore does not connect anything on ``profile``. The per-page hook
    is installed by :func:`install_cert_policy_on_page` from
    ``webview.WebView.__init__``; the profile argument is kept so the
    window can call this from its profile-created listener and so a
    future Qt that does add a profile-level signal gets wired too.

    Scope reminder: even the page hook only runs on the certificate
    *error* path. It cannot see a CA-valid wrong-key certificate. See the
    module docstring.
    """
    set_active_pin_store(store)
    signal = getattr(profile, "certificateError", None)
    if signal is None:
        log.info("profile %s: no certificateError signal (expected; the "
                 "signal is page-level and pages are wired individually)",
                 getattr(profile, "storageName", lambda: "?")())
        return
    _connect_cert_error(signal, store, "profile")


def install_cert_policy_on_page(page, store: PinStore | None = None) -> None:
    """Connect the error-path pin hook on one ``QWebEnginePage``.

    Must be called for every page the browser creates (there is exactly
    one creation site, ``webview.WebView.__init__``; qdbrowser does not
    override ``createWindow``, so Qt's default returns no page for
    popups and they are blocked rather than created unwired).

    With ``store=None`` the handler looks up the active store when the
    error fires, so pages built before ``install_cert_policy`` ran are
    still covered.
    """
    signal = getattr(page, "certificateError", None)
    if signal is None:
        log.error("page %r has no certificateError signal; the error-path "
                  "pin hook is NOT installed (Qt rejects unanswered "
                  "certificate errors, but pin violations will not be "
                  "journaled)", page)
        return
    _connect_cert_error(signal, store, "page")


def _connect_cert_error(signal, store: PinStore | None, what: str) -> None:
    """Connect ``_on_error`` to ``signal``.

    Decision table (Qt 6: an unanswered, undeferred error is rejected by
    QtWebEngine itself; qdbrowser never calls ``acceptCertificate``):

      no active store            -> leave unanswered (Qt rejects)
      host overridden            -> leave unanswered (Qt rejects)
      host not pinned            -> leave unanswered (Qt rejects)
      pinned, chain matches pin  -> leave unanswered (Qt rejects)
      pinned, mismatch/no certs  -> explicit rejectCertificate() + journal

    The explicit reject on the pinned path is what makes the decision
    ours rather than Qt's default, and it is the line the journal
    assertion keys on. If ``rejectCertificate()`` itself raises we log at
    ERROR and return without accepting; the request then falls to Qt's
    reject-by-default.
    """
    def _on_error(error):
        st = store if store is not None else _ACTIVE_STORE
        try:
            host = error.url().host()
        except Exception:
            host = ""
        if st is None:
            log.info("qdbrowser.cert default-handling host=%s (no pin store)",
                     host)
            return
        chain: list = []
        if hasattr(error, "certificateChain"):
            try:
                chain = [bytes(c.toDer())
                         for c in error.certificateChain()
                         if hasattr(c, "toDer")]
            except Exception as exc:
                log.warning("qdbrowser.cert chain unavailable host=%s: %s",
                            host, exc)
                chain = []
        if st.is_pinned(host):
            decision = st.evaluate(host, chain)
            if not decision.allow:
                log.warning("qdbrowser.cert reject host=%s reason=%s",
                            host, decision.kind)
                try:
                    error.rejectCertificate()
                except Exception as exc:
                    # Never fall through to accept. Qt rejects an
                    # unanswered error, so returning here is still a
                    # deny; the ERROR line makes the anomaly visible.
                    log.error("qdbrowser.cert rejectCertificate failed "
                              "host=%s: %s (load still denied: Qt rejects "
                              "unanswered errors)", host, exc)
                return
        # Not pinned / overridden / pin matched: leave the error
        # unanswered. QtWebEngine rejects it (no override UI exists in
        # Qt 6 unless the app builds one; qdbrowser does not).
        log.info("qdbrowser.cert default-handling host=%s", host)

    try:
        signal.connect(_on_error)
    except Exception as exc:
        log.error("could not connect certificateError on %s: %s", what, exc)
        return
    # Wiring evidence for the VM scenario (tests/integration/vm/
    # qdbrowser-cert-pin.bats): an unwired hook is otherwise invisible
    # because Qt rejects unanswered errors anyway. INFO so it shows with
    # QDBROWSER_LOG_LEVEL=INFO.
    log.info("qdbrowser.cert hook connected on %s", what)
