"""Tests for Phase 3 cross-user send-to pieces that don't need a live bus.

Covered here:
- `_SERVICE_NAME_RE` accepts well-formed com.qdistro.* names and
  rejects obvious injection shapes.
- The one_shot semantics in `_Request`: the flag plumbs through
  and scope validation rejects non-'once' scopes at DecideRequest-time.
- SDK friendly-name extraction (via qdistro_user_relay._friendly_name)
  strips the prefix and uid suffix as documented.

The RelayMessage/ListReceivers end-to-end behavior needs a real
system bus + user session buses; that's validated in-VM by the
GUI scenarios under `tests/integration/permissions-gui/11-*` and `12-*`. These
tests keep the pure-Python side of the contract honest.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _stub_dbus_and_paths():
    """Host-side tests don't have dbus-python installed; the broker
    module does `import dbus` at top level. Stub just enough to let
    the module load for the pure-Python constant/class assertions in
    this file. If a real dbus-python is already present, leave it.
    """
    p = Path(__file__).resolve().parents[1] / "user_relay"
    sys.path.insert(0, str(p))
    installed = []
    for name in ("dbus", "dbus.service", "dbus.mainloop",
                 "dbus.mainloop.glib", "dbus.bus",
                 "gi", "gi.repository"):
        if name in sys.modules:
            continue
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        installed.append(name)
    # Fill the shapes the broker module touches at import time only.
    if installed:
        dbus_mod = sys.modules["dbus"]
        service_mod = sys.modules["dbus.service"]
        dbus_mod.service = service_mod
        dbus_mod.mainloop = sys.modules["dbus.mainloop"]
        dbus_mod.mainloop.glib = sys.modules["dbus.mainloop.glib"]
        dbus_mod.bus = sys.modules["dbus.bus"]
        # `class Broker(dbus.service.Object)` forces us to produce a
        # real base class; a plain object works.
        service_mod.Object = object

        def _method(*_a, **_kw):
            return lambda fn: fn
        service_mod.method = _method

        def _signal(*_a, **_kw):
            return lambda fn: fn
        service_mod.signal = _signal

        class _BusName:
            def __init__(self, *_a, **_kw): pass
        service_mod.BusName = _BusName

        class _DBusException(Exception):
            def __init__(self, msg="", name=""):
                super().__init__(msg)
                self._name = name
            def get_dbus_name(self): return self._name
            def get_dbus_message(self): return str(self)
        dbus_mod.DBusException = _DBusException
        dbus_mod.SystemBus = lambda: None
        dbus_mod.SessionBus = lambda: None
        dbus_mod.Int32 = lambda v: v
        dbus_mod.Int64 = lambda v: v
        dbus_mod.String = lambda v: v
        dbus_mod.Boolean = lambda v: v
        dbus_mod.Dictionary = lambda v, signature=None: v
        dbus_mod.Array = lambda v, signature=None: list(v)

        gi_repo = sys.modules["gi.repository"]
        class _GLib:
            @staticmethod
            def timeout_add_seconds(*_a, **_kw): return 0
            class MainLoop:
                def run(self): pass
        gi_repo.GLib = _GLib
    yield
    for name in installed:
        sys.modules.pop(name, None)
    try:
        sys.path.remove(str(p))
    except ValueError:
        pass


def test_service_name_regex_accepts_typical_shapes():
    from qdistro_admin_broker import _SERVICE_NAME_RE
    assert _SERVICE_NAME_RE.match("com.qdistro.StubNotepad.uid3000")
    assert _SERVICE_NAME_RE.match("com.qdistro.StubNotepad")
    assert _SERVICE_NAME_RE.match("com.qdistro.Foo.Bar.Baz")


@pytest.mark.parametrize("bad", [
    "",
    "org.freedesktop.DBus",
    "com.qdistro.",           # trailing dot
    "com.qdistro..Double",    # empty component
    "com.qdistro.Name-dash",  # dash not allowed
    "com.evil;rm -rf /",      # shell injection-ish
    "com.qdistro.Name\nEOF",  # newline
])
def test_service_name_regex_rejects_garbage(bad):
    from qdistro_admin_broker import _SERVICE_NAME_RE
    assert not _SERVICE_NAME_RE.match(bad)


def test_request_one_shot_flag_defaults_false():
    from qdistro_admin_broker import _Request
    r = _Request(1, 2000, 100, "/x", 0, "a", {})
    assert r.one_shot is False
    assert r.delegated is False


def test_request_one_shot_flag_true():
    from qdistro_admin_broker import _Request
    r = _Request(1, 2000, 100, "/x", 0, "a", {}, one_shot=True)
    assert r.one_shot is True


def test_oneshot_and_delegated_share_argv_blind_bans():
    """task(078): argv-blind scopes (forever / forever_exe / 1h / 24h)
    are forbidden for both delegated and one-shot. argv-aware scopes
    (forever_argv / forever_basename / forever_prefix) are FORBIDDEN
    for one-shot (target_uid+target_service is too fine-grained for any
    persistent grant to be useful) but ALLOWED for delegated (argv
    pinning is the qsu argv-leak fix)."""
    from qdistro_admin_broker import (
        _DELEGATED_FORBIDDEN_SCOPES, _ONESHOT_FORBIDDEN_SCOPES,
    )
    argv_blind = frozenset(("1h", "24h", "forever", "forever_exe"))
    argv_aware = frozenset((
        "forever_argv", "forever_basename", "forever_prefix"))

    # Both ban the argv-blind set.
    assert argv_blind <= _DELEGATED_FORBIDDEN_SCOPES
    assert argv_blind <= _ONESHOT_FORBIDDEN_SCOPES

    # Delegated PERMITS argv-aware scopes after task(078).
    for s in argv_aware:
        assert s not in _DELEGATED_FORBIDDEN_SCOPES, \
            f"delegated should permit argv-aware {s!r}"

    # One-shot still forbids argv-aware.
    for s in argv_aware:
        assert s in _ONESHOT_FORBIDDEN_SCOPES, \
            f"one-shot should still forbid argv-aware {s!r}"

    # `once` is never in either set.
    assert "once" not in _DELEGATED_FORBIDDEN_SCOPES
    assert "once" not in _ONESHOT_FORBIDDEN_SCOPES


@pytest.mark.parametrize("service,expected", [
    ("com.qdistro.StubNotepad.uid3000", "StubNotepad"),
    ("com.qdistro.StubNotepad", "StubNotepad"),
    ("com.qdistro.Foo.uid1", "Foo"),
    ("com.qdistro.Foo.Bar", "Foo.Bar"),              # not a uidN suffix
    ("com.qdistro.Foo.uidABC", "Foo.uidABC"),        # non-numeric suffix
    ("com.notqdistro.X", "com.notqdistro.X"),        # unchanged if prefix absent
])
def test_friendly_name_strips_prefix_and_uid(service, expected):
    from qdistro_user_relay import _friendly_name
    assert _friendly_name(service) == expected


def test_user_relay_excludes_its_own_name():
    from qdistro_user_relay import BUS_NAME, EXCLUDED_NAMES
    assert BUS_NAME in EXCLUDED_NAMES
