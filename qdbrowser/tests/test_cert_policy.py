"""Cert-pin store: SPKI hashing, PinStore, evaluate, loader, error hook.

Most tests are pure-Python. The hook tests (``TestErrorPathHook``) use
fake page/error objects so they assert the decision table without
Chromium; ``TestRuntimeProbe`` checks the real PyQt6 API shape (skipped
when PyQt6 is unavailable). No integration scenario exercises the hook
end to end yet — see iso2 `13` E2.
"""

import base64
import hashlib
import json
import os
from datetime import UTC

import pytest

# ---------------------------------------------------------------------------
# Fixture cert: generate a minimal self-signed DER at test-collection time.
# ---------------------------------------------------------------------------


def _make_cert_der():
    """Build a self-signed DER cert and return (der_bytes, expected_pin)."""
    pytest.importorskip("cryptography")
    from datetime import datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "test.example.com"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    spki = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(spki).digest()
    expected = "sha256/" + base64.b64encode(digest).decode("ascii")
    return der, expected


@pytest.fixture(scope="module")
def cert_fixture():
    return _make_cert_der()


# ---------------------------------------------------------------------------
# spki_hash_from_der
# ---------------------------------------------------------------------------


def test_spki_hash_from_der_matches_known_value(cert_fixture):
    from qdbrowser.cert_policy import spki_hash_from_der
    der, expected = cert_fixture
    got = spki_hash_from_der(der)
    assert got == expected


def test_spki_hash_from_der_empty_returns_none():
    from qdbrowser.cert_policy import spki_hash_from_der
    assert spki_hash_from_der(b"") is None
    assert spki_hash_from_der(None) is None


def test_spki_hash_from_der_garbage_returns_none():
    from qdbrowser.cert_policy import spki_hash_from_der
    # Not a SEQUENCE — first byte not 0x30.
    assert spki_hash_from_der(b"\x02\x01\x05") is None
    # Truncated SEQUENCE
    assert spki_hash_from_der(b"\x30\x82\xff\xff") is None


# ---------------------------------------------------------------------------
# PinStore basics
# ---------------------------------------------------------------------------


def test_pinstore_normalizes_host_keys():
    from qdbrowser.cert_policy import PinStore
    store = PinStore(pins={"Example.COM": ["sha256/AAAA"]})
    assert store.is_pinned("example.com")
    assert store.is_pinned("EXAMPLE.com")
    assert "example.com" in store.pinned_hosts


def test_pinstore_drops_malformed_pins():
    from qdbrowser.cert_policy import PinStore
    store = PinStore(pins={
        "good.com": ["sha256/abc", "not-a-pin", 42],
        "empty.com": ["bad"],   # all-bad => dropped entirely
        "none.com": None,
    })
    assert store.pins_for("good.com") == ["sha256/abc"]
    assert store.pins_for("empty.com") == []
    assert not store.is_pinned("empty.com")
    assert not store.is_pinned("none.com")


def test_pinstore_overrides_normalized():
    from qdbrowser.cert_policy import PinStore
    store = PinStore(overrides=["FOO.com", "bar.com", 42, None])
    assert store.is_overridden("foo.com")
    assert store.is_overridden("BAR.COM")
    assert store.overridden_hosts == ["bar.com", "foo.com"]


def test_pinstore_pins_for_unknown_host():
    from qdbrowser.cert_policy import PinStore
    store = PinStore(pins={"a.com": ["sha256/abc"]})
    assert store.pins_for("nothing.com") == []
    assert store.pins_for("") == []


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_unpinned_host_is_ok(cert_fixture):
    from qdbrowser.cert_policy import PinDecision, PinStore
    der, _ = cert_fixture
    store = PinStore()
    d = store.evaluate("nothing.example.com", [der])
    assert d.kind == PinDecision.OK
    assert d.allow is True


def test_evaluate_matching_cert_allows(cert_fixture):
    from qdbrowser.cert_policy import PinDecision, PinStore
    der, expected = cert_fixture
    store = PinStore(pins={"test.example.com": [expected]})
    d = store.evaluate("test.example.com", [der])
    assert d.kind == PinDecision.OK
    assert d.allow is True
    assert expected in d.detail


def test_evaluate_mismatched_cert_rejects(cert_fixture, caplog):
    import logging

    from qdbrowser.cert_policy import PinDecision, PinStore
    der, _ = cert_fixture
    store = PinStore(pins={"test.example.com": ["sha256/AAAAdeadbeef"]})
    with caplog.at_level(logging.WARNING, logger="qdbrowser.cert"):
        d = store.evaluate("test.example.com", [der])
    assert d.kind == PinDecision.PIN_MISMATCH
    assert d.allow is False
    # Journal line is the load-bearing assertion.
    assert any("pin_violation" in r.message for r in caplog.records)


def test_evaluate_pinned_but_overridden_accepts(cert_fixture, caplog):
    import logging

    from qdbrowser.cert_policy import PinDecision, PinStore
    der, _ = cert_fixture
    store = PinStore(
        pins={"test.example.com": ["sha256/wrong"]},
        overrides=["test.example.com"],
    )
    with caplog.at_level(logging.WARNING, logger="qdbrowser.cert"):
        d = store.evaluate("test.example.com", [der])
    assert d.kind == PinDecision.OVERRIDDEN
    assert d.allow is True
    assert any("override active" in r.message for r in caplog.records)


def test_evaluate_no_certs_when_pinned():
    from qdbrowser.cert_policy import PinDecision, PinStore
    store = PinStore(pins={"a.com": ["sha256/abc"]})
    d = store.evaluate("a.com", [])
    assert d.kind == PinDecision.NO_CERTS
    assert d.allow is False


def test_evaluate_intermediate_matches(cert_fixture):
    """If a non-leaf cert matches, the chain is accepted."""
    from qdbrowser.cert_policy import PinDecision, PinStore
    der, expected = cert_fixture
    # Leaf is garbage DER (returns None hash), intermediate is the real cert.
    store = PinStore(pins={"test.example.com": [expected]})
    # First entry is a non-cert blob — its hash is None and skipped.
    d = store.evaluate("test.example.com", [b"\x00\x01garbage", der])
    assert d.kind == PinDecision.OK


# ---------------------------------------------------------------------------
# load_pin_store
# ---------------------------------------------------------------------------


def test_load_pin_store_missing_files(tmp_path):
    from qdbrowser.cert_policy import load_pin_store
    store = load_pin_store(
        system_path=str(tmp_path / "no-system.json"),
        user_path=str(tmp_path / "no-user.json"),
        overrides_path=str(tmp_path / "no-over.json"),
    )
    assert store.pinned_hosts == []
    assert store.overridden_hosts == []


def test_load_pin_store_user_overrides_system(tmp_path):
    from qdbrowser.cert_policy import load_pin_store
    sys_path = tmp_path / "system.json"
    user_path = tmp_path / "user.json"
    over_path = tmp_path / "over.json"
    sys_path.write_text(json.dumps({
        "a.com": ["sha256/sysA"],
        "b.com": ["sha256/sysB"],
    }))
    user_path.write_text(json.dumps({
        "a.com": ["sha256/userA"],  # overrides system
        "c.com": ["sha256/userC"],  # net-new
    }))
    over_path.write_text(json.dumps(["override.com"]))

    store = load_pin_store(
        system_path=str(sys_path),
        user_path=str(user_path),
        overrides_path=str(over_path),
    )
    assert store.pins_for("a.com") == ["sha256/userA"]
    assert store.pins_for("b.com") == ["sha256/sysB"]
    assert store.pins_for("c.com") == ["sha256/userC"]
    assert store.is_overridden("override.com")


def test_load_pin_store_overrides_hosts_dict_form(tmp_path):
    from qdbrowser.cert_policy import load_pin_store
    over_path = tmp_path / "over.json"
    over_path.write_text(json.dumps({"hosts": ["a.com", "b.com"]}))
    store = load_pin_store(overrides_path=str(over_path))
    assert store.is_overridden("a.com")
    assert store.is_overridden("b.com")


def test_load_pin_store_malformed_json_treated_as_empty(tmp_path, caplog):
    import logging

    from qdbrowser.cert_policy import load_pin_store
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="qdbrowser.cert"):
        store = load_pin_store(system_path=str(bad))
    assert store.pinned_hosts == []
    assert any("unreadable" in r.message for r in caplog.records)


def test_load_pin_store_dev_override_under_home(tmp_path, monkeypatch):
    """User-scope override file in ~/.config/qdbrowser/cert-pins.json wins.

    Use $HOME-driven path expansion to exercise the dev-iteration code path.
    """
    from qdbrowser.cert_policy import load_pin_store

    monkeypatch.setenv("HOME", str(tmp_path))
    user_dir = tmp_path / ".config" / "qdbrowser"
    user_dir.mkdir(parents=True)
    user_path = user_dir / "cert-pins.json"
    user_path.write_text(json.dumps({"dev.example.com": ["sha256/devpin"]}))

    # Now use os.path.expanduser to derive the user_path that production
    # code would derive.
    derived = os.path.expanduser("~/.config/qdbrowser/cert-pins.json")
    assert derived == str(user_path)

    store = load_pin_store(user_path=derived)
    assert store.pins_for("dev.example.com") == ["sha256/devpin"]


# ---------------------------------------------------------------------------
# Error-path hook (iso2 `13` E2). The signal lives on QWebEnginePage, never
# on QWebEngineProfile; these fakes model exactly that.
# ---------------------------------------------------------------------------


class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _FakePage:
    def __init__(self):
        self.certificateError = _FakeSignal()


class _FakeProfile:
    """No ``certificateError`` — true of every Qt release."""

    def storageName(self):
        return "fake"


class _FakeCert:
    def __init__(self, der):
        self._der = der

    def toDer(self):
        return self._der


class _FakeUrl:
    def __init__(self, host):
        self._host = host

    def host(self):
        return self._host


class _FakeError:
    def __init__(self, host, ders, reject_raises=False):
        self._url = _FakeUrl(host)
        self._chain = [_FakeCert(d) for d in ders]
        self.accepted = 0
        self.rejected = 0
        self._reject_raises = reject_raises

    def url(self):
        return self._url

    def certificateChain(self):
        return list(self._chain)

    def acceptCertificate(self):
        self.accepted += 1

    def rejectCertificate(self):
        if self._reject_raises:
            raise RuntimeError("boom")
        self.rejected += 1


@pytest.fixture(autouse=True)
def _reset_active_store():
    from qdbrowser import cert_policy
    # getattr: keeps the pre-existing pure-Python tests runnable against a
    # tree that predates the page hook, so only the hook tests fail there.
    reset = getattr(cert_policy, "set_active_pin_store", lambda _s: None)
    reset(None)
    yield
    reset(None)


class TestErrorPathHook:
    def test_profile_install_registers_store_but_wires_nothing(self, caplog):
        """The old code read ``profile.certificateError`` and silently
        returned. The profile has no such signal; the store must still be
        registered for the page hook."""
        import logging

        from qdbrowser.cert_policy import (
            PinStore,
            active_pin_store,
            install_cert_policy,
        )
        store = PinStore(pins={"bank.example.com": ["sha256/x"]})
        with caplog.at_level(logging.INFO, logger="qdbrowser.cert"):
            install_cert_policy(_FakeProfile(), store)
        assert active_pin_store() is store
        assert any("page-level" in r.message for r in caplog.records)

    def test_page_hook_is_connected_on_the_page(self):
        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        page = _FakePage()
        install_cert_policy_on_page(page, PinStore())
        assert len(page.certificateError.slots) == 1

    def test_pinned_mismatch_rejects_and_never_accepts(self, cert_fixture,
                                                        caplog):
        import logging

        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        der, _real_pin = cert_fixture
        store = PinStore(pins={"bank.example.com": ["sha256/notthisone"]})
        page = _FakePage()
        install_cert_policy_on_page(page, store)
        err = _FakeError("bank.example.com", [der])
        with caplog.at_level(logging.WARNING, logger="qdbrowser.cert"):
            page.certificateError.emit(err)
        assert err.rejected == 1
        assert err.accepted == 0
        assert any("qdbrowser.cert reject host=bank.example.com" in r.message
                   for r in caplog.records)

    def test_pinned_no_certs_rejects(self):
        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        store = PinStore(pins={"bank.example.com": ["sha256/x"]})
        page = _FakePage()
        install_cert_policy_on_page(page, store)
        err = _FakeError("bank.example.com", [])
        page.certificateError.emit(err)
        assert (err.rejected, err.accepted) == (1, 0)

    def test_reject_failure_logs_error_and_does_not_accept(self, cert_fixture,
                                                            caplog):
        """Old code: ``except Exception: pass``. Now: ERROR log, no accept."""
        import logging

        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        der, _ = cert_fixture
        store = PinStore(pins={"bank.example.com": ["sha256/notthisone"]})
        page = _FakePage()
        install_cert_policy_on_page(page, store)
        err = _FakeError("bank.example.com", [der], reject_raises=True)
        with caplog.at_level(logging.ERROR, logger="qdbrowser.cert"):
            page.certificateError.emit(err)
        assert err.accepted == 0
        assert any(r.levelno == logging.ERROR
                   and "rejectCertificate failed" in r.message
                   for r in caplog.records)

    def test_pinned_match_is_left_to_qt_default(self, cert_fixture):
        """A matching pin does not mean accept: qdbrowser never calls
        acceptCertificate; Qt 6 rejects the unanswered error."""
        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        der, pin = cert_fixture
        store = PinStore(pins={"bank.example.com": [pin]})
        page = _FakePage()
        install_cert_policy_on_page(page, store)
        err = _FakeError("bank.example.com", [der])
        page.certificateError.emit(err)
        assert (err.rejected, err.accepted) == (0, 0)

    def test_unpinned_host_is_left_unanswered(self, cert_fixture):
        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        der, _ = cert_fixture
        store = PinStore(pins={"bank.example.com": ["sha256/x"]})
        page = _FakePage()
        install_cert_policy_on_page(page, store)
        err = _FakeError("other.example.com", [der])
        page.certificateError.emit(err)
        assert (err.rejected, err.accepted) == (0, 0)

    def test_page_wired_before_store_uses_active_store(self, cert_fixture):
        """Pages built before the window loaded pins resolve the store at
        error time."""
        from qdbrowser.cert_policy import (
            PinStore,
            install_cert_policy,
            install_cert_policy_on_page,
        )
        der, _ = cert_fixture
        page = _FakePage()
        install_cert_policy_on_page(page)          # store=None
        err0 = _FakeError("bank.example.com", [der])
        page.certificateError.emit(err0)
        assert (err0.rejected, err0.accepted) == (0, 0)   # no store yet
        install_cert_policy(_FakeProfile(),
                            PinStore(pins={"bank.example.com": ["sha256/x"]}))
        err1 = _FakeError("bank.example.com", [der])
        page.certificateError.emit(err1)
        assert (err1.rejected, err1.accepted) == (1, 0)

    def test_page_without_signal_logs_error(self, caplog):
        import logging

        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        with caplog.at_level(logging.ERROR, logger="qdbrowser.cert"):
            install_cert_policy_on_page(object(), PinStore())
        assert any("NOT installed" in r.message for r in caplog.records)


class TestRuntimeProbe:
    """The code relies on ``certificateError`` being a page signal. Pin the
    real API shape so a Qt that moves it fails loudly here."""

    def test_signal_is_on_page_not_profile(self):
        core = pytest.importorskip("PyQt6.QtWebEngineCore")
        assert hasattr(core.QWebEnginePage, "certificateError")
        assert not hasattr(core.QWebEngineProfile, "certificateError")

    def test_no_success_path_certificate_api(self):
        """Documents the E2 architectural gap: nothing in QtWebEngine
        exposes the peer chain outside the error object."""
        core = pytest.importorskip("PyQt6.QtWebEngineCore")
        assert hasattr(core.QWebEngineCertificateError, "certificateChain")
        for cls in (core.QWebEngineUrlRequestInfo, core.QWebEngineLoadingInfo,
                    core.QWebEngineProfile):
            assert not [n for n in dir(cls)
                        if "certificatechain" in n.lower()
                        or "peercertificate" in n.lower()], cls

    def test_real_page_gets_hook(self, qapp, caplog):
        import logging

        core = pytest.importorskip("PyQt6.QtWebEngineCore")
        from qdbrowser.cert_policy import PinStore, install_cert_policy_on_page
        prof = core.QWebEngineProfile()          # off-the-record
        page = core.QWebEnginePage(prof)
        try:
            with caplog.at_level(logging.ERROR, logger="qdbrowser.cert"):
                install_cert_policy_on_page(page, PinStore())
            assert not caplog.records
        finally:
            page.deleteLater()
            prof.deleteLater()
            qapp.processEvents()

    @pytest.mark.parametrize("profile_name", ["default", "private"])
    def test_webview_wires_every_page(self, qapp, monkeypatch, profile_name):
        """Every page WebView creates — persistent or off-the-record —
        passes through install_cert_policy_on_page."""
        pytest.importorskip("PyQt6.QtWebEngineWidgets")
        from qdbrowser import cert_policy
        from qdbrowser.webview import WebView
        seen = []
        monkeypatch.setattr(cert_policy, "install_cert_policy_on_page",
                            lambda page, store=None: seen.append(page))
        wv = WebView(profile_name=profile_name)
        try:
            assert seen == [wv.view.page()]
        finally:
            wv.deleteLater()
            qapp.processEvents()
