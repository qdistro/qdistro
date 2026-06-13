"""Unit tests for AuthBackend._pam_worker — the PAM unlock decision.

`pam.authenticate()` is the actual password gate behind the locker. It
is reached via a lazy `import pam` inside `_pam_worker`, so — exactly as
test_auth_fprintd.py does for dbus-next — we inject a FAKE `pam` module
via `sys.modules` and drive `_pam_worker()` directly on its worker thread
machinery.

The invariants under test are fail-CLOSED ones: a wrong password, a
raised authenticate(), or a missing python-pam must NEVER emit SUCCESS,
and a correct password must emit SUCCESS tagged with the session
generation that was current when the attempt ran.
"""

from __future__ import annotations

import os
import pwd
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("USER", "tester")

import pytest
from qdlocker.auth import AuthBackend, AuthOutcome

# ---- fake `pam` plumbing ----------------------------------------------------


def _install_fake_pam(monkeypatch, *, authenticate):
    """Install a fake top-level `pam` module whose `pam().authenticate`
    is the supplied callable. Mirrors _install_fake_dbus in
    test_auth_fprintd.py — the real python-pam need not be installed."""
    captured = {"calls": []}

    class _FakePam:
        def authenticate(self, user, password, service=None, call_end=True):
            captured["calls"].append(
                {
                    "user": user,
                    "password": password,
                    "service": service,
                    "call_end": call_end,
                }
            )
            return authenticate(user, password, service)

    mod = types.ModuleType("pam")
    mod.pam = _FakePam
    monkeypatch.setitem(sys.modules, "pam", mod)
    return captured


def _uninstall_pam(monkeypatch):
    """Force `import pam` inside the worker to raise ImportError, even if
    python-pam happens to be installed on the host."""
    # A finder that intercepts only `pam` and refuses it.
    monkeypatch.setitem(sys.modules, "pam", None)


def _make_backend():
    # fprintd disabled — we are exercising only the PAM path.
    return AuthBackend(fprintd_enabled=False)


def _run_pam_worker(backend, password):
    """Drive _pam_worker synchronously in THIS thread.

    The worker normally runs on a background thread, but it only blocks
    in the password-wait loop; pre-supplying the password (or setting the
    abort event) lets that loop exit on its first iteration so the body
    runs straight through. Running in-thread also means the `outcome`
    pyqtSignal is delivered synchronously to a directly-connected Python
    slot — exactly as test_auth_fprintd.py relies on for _fprint_async.
    A real Qt event loop would otherwise be needed to pump a queued
    cross-thread emit."""
    if password is not None:
        backend.respond_pam(password)
    backend._pam_worker()


# ---- the four core decisions ------------------------------------------------


@pytest.mark.cheat_aware(
    protects="a wrong password is rejected — _pam_worker emits FAILED, "
    "never SUCCESS, so the locked screen stays locked",
    severity="critical",
    cheats=[
        "assert on a stub instead of the real authenticate() return value",
        "treat a falsy/None authenticate() result as success",
        "swallow the outcome and only check that the worker did not raise",
    ],
    consequence="any password (or none) unlocks the session — total auth "
    "bypass of the screen locker",
)
def test_wrong_password_emits_failed(monkeypatch):
    _install_fake_pam(monkeypatch, authenticate=lambda u, p, s: False)
    backend = _make_backend()

    emitted = []
    backend.outcome.connect(lambda payload: emitted.append(payload))

    _run_pam_worker(backend, "wrong-password")

    gen = backend._current_generation()
    assert (AuthOutcome.FAILED, gen) in emitted
    assert all(p[0] is not AuthOutcome.SUCCESS for p in emitted)


def test_correct_password_emits_tagged_success(monkeypatch):
    captured = _install_fake_pam(monkeypatch, authenticate=lambda u, p, s: True)
    backend = _make_backend()
    # Bump the generation so we assert the real (non-zero) value flows
    # through to the emitted outcome.
    backend.reset_session()
    gen = backend._current_generation()
    assert gen != 0

    emitted = []
    backend.outcome.connect(lambda payload: emitted.append(payload))

    _run_pam_worker(backend, "correct-password")

    assert (AuthOutcome.SUCCESS, gen) in emitted
    # The real password/user reached authenticate() (not a stub bypass).
    assert captured["calls"], "authenticate() was never called"
    call = captured["calls"][0]
    assert call["password"] == "correct-password"
    assert call["user"] == backend._pam_user
    assert call["call_end"] is True


def test_abort_before_password_emits_aborted(monkeypatch):
    # authenticate() must never even be reached when aborted first.
    captured = _install_fake_pam(
        monkeypatch,
        authenticate=lambda u, p, s: pytest.fail("authenticate ran after abort"),
    )
    backend = _make_backend()

    emitted = []
    backend.outcome.connect(lambda payload: emitted.append(payload))

    backend.abort_pam()  # set the abort event before the worker waits
    _run_pam_worker(backend, password=None)

    gen = backend._current_generation()
    assert (AuthOutcome.ABORTED, gen) in emitted
    assert all(p[0] is not AuthOutcome.SUCCESS for p in emitted)
    assert captured["calls"] == []


@pytest.mark.cheat_aware(
    protects="if python-pam is not importable, _pam_worker fails CLOSED — "
    "emits FAILED and NEVER SUCCESS, so a missing/broken PAM dependency "
    "cannot leave the screen unlockable-by-default",
    severity="critical",
    cheats=[
        "default the outcome to SUCCESS when the import fails",
        "skip emitting any outcome (leaving the UI in an ambiguous state)",
        "catch ImportError and retry with a permissive auth path",
    ],
    consequence="on a host missing python-pam the locker would unlock "
    "without ever checking a password — a fail-OPEN auth bypass",
)
def test_import_error_fails_closed(monkeypatch):
    _uninstall_pam(monkeypatch)
    backend = _make_backend()

    emitted = []
    backend.outcome.connect(lambda payload: emitted.append(payload))

    _run_pam_worker(backend, password=None)

    gen = backend._current_generation()
    assert (AuthOutcome.FAILED, gen) in emitted
    assert all(p[0] is not AuthOutcome.SUCCESS for p in emitted)


@pytest.mark.cheat_aware(
    protects="an exception thrown mid-authenticate() fails CLOSED — the "
    "worker emits FAILED, never SUCCESS, so a crashing PAM stack cannot "
    "unlock the screen",
    severity="critical",
    cheats=[
        "let the exception escape and treat 'did not return' as success",
        "assert only that the worker raised, not that it emitted FAILED",
    ],
    consequence="a PAM stack that errors out unlocks the session — auth "
    "bypass via induced failure",
)
def test_authenticate_raises_fails_closed(monkeypatch):
    # A PAM module / stack that throws mid-authenticate must fail CLOSED.
    def boom(u, p, s):
        raise RuntimeError("PAM stack exploded")

    _install_fake_pam(monkeypatch, authenticate=boom)
    backend = _make_backend()

    emitted = []
    backend.outcome.connect(lambda payload: emitted.append(payload))

    _run_pam_worker(backend, "whatever")

    gen = backend._current_generation()
    assert (AuthOutcome.FAILED, gen) in emitted
    assert all(p[0] is not AuthOutcome.SUCCESS for p in emitted)


@pytest.mark.cheat_aware(
    protects="a late PAM result arriving after the session was aborted is "
    "reported as ABORTED, never leaked as SUCCESS/FAILED for the wrong "
    "generation",
    severity="high",
    cheats=[
        "ignore the generation and emit the raw authenticate() outcome",
        "assert only that SOMETHING was emitted, not that it was ABORTED",
    ],
    consequence="a stale unlock decision from a superseded attempt unlocks "
    "(or wrongly rejects) the current session",
)
def test_abort_after_authenticate_emits_aborted(monkeypatch):
    # If the session is aborted (e.g. fprintd unlocked) while authenticate()
    # was running, the late PAM result must be reported as ABORTED, not
    # leaked as SUCCESS/FAILED.
    def auth_then_abort(u, p, s):
        backend.abort_pam()
        return True  # would otherwise be SUCCESS

    _install_fake_pam(monkeypatch, authenticate=auth_then_abort)
    backend = _make_backend()

    emitted = []
    backend.outcome.connect(lambda payload: emitted.append(payload))

    _run_pam_worker(backend, "pw")

    gen = backend._current_generation()
    assert (AuthOutcome.ABORTED, gen) in emitted
    assert all(p[0] is not AuthOutcome.SUCCESS for p in emitted)


def test_authenticate_receives_resolved_service(monkeypatch):
    # The service resolved by probe_pam() must be threaded into
    # authenticate(); a None service falls back to "login".
    captured = _install_fake_pam(monkeypatch, authenticate=lambda u, p, s: True)
    backend = _make_backend()
    backend._pam_service = "system-auth"

    _run_pam_worker(backend, "pw")
    assert captured["calls"][0]["service"] == "system-auth"


def test_authenticate_defaults_service_to_login(monkeypatch):
    captured = _install_fake_pam(monkeypatch, authenticate=lambda u, p, s: True)
    backend = _make_backend()
    backend._pam_service = None  # unresolved

    _run_pam_worker(backend, "pw")
    assert captured["calls"][0]["service"] == "login"


# ---- service / user resolution (probe_pam, auth.py:134-147) -----------------


def test_probe_pam_uses_env_service(monkeypatch):
    monkeypatch.setenv("QDLOCKER_PAM_SERVICE", "my-service")
    monkeypatch.setenv("USER", "tester")
    backend = AuthBackend(fprintd_enabled=False)
    assert backend._pam_service == "my-service"

    ready = []
    backend.ready.connect(lambda: ready.append(True))
    backend.probe_pam()
    # Env service short-circuits detection and still signals ready.
    assert backend._pam_service == "my-service"
    assert ready == [True]


def test_probe_pam_detects_existing_service_file(monkeypatch, tmp_path):
    monkeypatch.delenv("QDLOCKER_PAM_SERVICE", raising=False)
    monkeypatch.setenv("USER", "tester")
    backend = AuthBackend(fprintd_enabled=False)
    assert backend._pam_service is None

    # Make only "system-auth" appear to exist (login probed first, missing).
    real_exists = os.path.exists

    def fake_exists(path):
        if path == "/etc/pam.d/login":
            return False
        if path == "/etc/pam.d/system-auth":
            return True
        return real_exists(path)

    monkeypatch.setattr(os.path, "exists", fake_exists)

    ready = []
    backend.ready.connect(lambda: ready.append(True))
    backend.probe_pam()
    assert backend._pam_service == "system-auth"
    assert ready == [True]


def test_probe_pam_defaults_to_login_when_none_found(monkeypatch):
    monkeypatch.delenv("QDLOCKER_PAM_SERVICE", raising=False)
    monkeypatch.setenv("USER", "tester")
    backend = AuthBackend(fprintd_enabled=False)

    monkeypatch.setattr(os.path, "exists", lambda path: False)

    ready = []
    backend.ready.connect(lambda: ready.append(True))
    backend.probe_pam()
    # Fail-safe default — a missing PAM config still yields a service name
    # (the actual auth will then fail closed if that service is bogus).
    assert backend._pam_service == "login"
    assert ready == [True]


def test_unresolvable_uid_raises(monkeypatch):
    # Identity is derived from the process uid via pwd.getpwuid(). If NSS /
    # the passwd database cannot resolve the running uid, construction must
    # fail CLOSED rather than fall back to anything weaker.
    def boom(_uid):
        raise KeyError("no such uid")

    monkeypatch.setattr(pwd, "getpwuid", boom)
    with pytest.raises(RuntimeError):
        AuthBackend(fprintd_enabled=False)


@pytest.mark.cheat_aware(
    protects="the authenticated account is derived from the process uid, "
    "never from the mutable USER/LOGNAME env — pwd.getpwuid(os.getuid()) "
    "is the only source of _pam_user",
    severity="critical",
    cheats=[
        "read USER/LOGNAME (again) to populate _pam_user",
        "assert _pam_user == os.environ['USER'] instead of the uid name",
        "fall back to getpass.getuser()/os.getlogin() (env/utmp-backed)",
    ],
    consequence="an attacker who can set USER/LOGNAME in the locker's "
    "environment redirects PAM/fprintd auth to a different account",
)
def test_username_from_uid_not_env(monkeypatch):
    monkeypatch.setenv("USER", "root")
    monkeypatch.setenv("LOGNAME", "root")
    backend = AuthBackend(fprintd_enabled=False)
    expected = pwd.getpwuid(os.getuid()).pw_name
    assert backend._pam_user == expected
    # Tampered env must not have redirected identity (unless we genuinely
    # run as root, in which case the uid name legitimately is "root").
    if os.getuid() != 0:
        assert backend._pam_user != "root"


def test_username_resolves_with_env_unset(monkeypatch):
    # Identity is env-independent: with USER and LOGNAME both unset,
    # construction still succeeds and yields the uid-derived name.
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    backend = AuthBackend(fprintd_enabled=False)
    assert backend._pam_user == pwd.getpwuid(os.getuid()).pw_name
