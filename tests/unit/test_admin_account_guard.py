"""The fixed-admin (uid 1000) invariant is enforced fail-closed at daemon
STARTUP, not at import.

qdistro is single-tenant: the admin role is the fixed 'admin' account, which
must be uid 1000. The daemons used to resolve+enforce this at MODULE IMPORT,
which made them unimportable on any host without that account (dev/CI boxes) and
broke unit-test collection. They now resolve ADMIN_UID leniently at import
(defaulting to 1000 when the account is absent, so tests can import them and the
suite's ADMIN_UID==1000 assumptions hold) and enforce the strict invariant in
each daemon's main() via _require_admin_account().

These tests pin that contract directly, so a regression that re-adds an
import-time hard-fail (breaking collection again) OR drops the runtime guard
(weakening the authz boundary) is caught here instead of in review.
"""
from __future__ import annotations

import importlib
import pwd as _pwd
import sys
from unittest import mock

import pytest

pytest.importorskip("dbus")  # the broker/pwd/polkit daemons need dbus-python

# qdistro_polkit_agent imports python-pam at module load; stub it so the import
# succeeds on hosts without it (mirrors test_polkit_agent_method.py).
sys.modules.setdefault("pam", mock.MagicMock())

# Every daemon that owns a fixed-admin invariant + a runtime guard.
DAEMON_MODULES = [
    "qdistro_admin_broker",
    "qdistro_pwd_daemon",
    "qdistro_polkit_agent",
    "qdistro_session_manager",
]


def _passwd(uid: int) -> _pwd.struct_passwd:
    return _pwd.struct_passwd(
        ("admin", "x", uid, uid, "admin", "/home/admin", "/bin/bash"))


@pytest.fixture(params=DAEMON_MODULES)
def daemon(request):
    return importlib.import_module(request.param)


def test_module_imports_with_lenient_admin_uid(daemon):
    """Importable on hosts without an 'admin' account; ADMIN_UID defaults to 1000."""
    assert daemon.ADMIN_UID == 1000
    assert callable(daemon._require_admin_account)


def test_guard_fails_closed_when_admin_absent(daemon, monkeypatch):
    """Missing admin account => the daemon refuses to start."""
    monkeypatch.setattr(_pwd, "getpwnam", mock.Mock(side_effect=KeyError("admin")))
    with pytest.raises(RuntimeError, match="does not exist"):
        daemon._require_admin_account()


def test_guard_fails_closed_on_wrong_uid(daemon, monkeypatch):
    """admin present but not uid 1000 => the daemon refuses to start."""
    monkeypatch.setattr(_pwd, "getpwnam", lambda _name: _passwd(1234))
    with pytest.raises(RuntimeError, match="must resolve to uid 1000"):
        daemon._require_admin_account()
