"""Privileged broker methods require trusted peer identity, not uid alone."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("dbus")
import dbus  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402
from test_broker_check_permission import (  # noqa: E402
    _StubBroker, ADMIN_UID, NON_ADMIN_UID, PEER_EXE,
)


ARBITRARY_ADMIN_EXE = "/usr/bin/python3"
DEAD_PID = 999999


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def broker(tmp_path: Path, rules_dir: Path) -> _StubBroker:
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


def _as_arbitrary_admin(broker: _StubBroker) -> None:
    broker.set_peer(uid=ADMIN_UID, pid=DEAD_PID, exe=ARBITRARY_ADMIN_EXE)


def test_arbitrary_uid_1000_cannot_decide_request(broker):
    rid = broker._enqueue(NON_ADMIN_UID, 1234, PEER_EXE, 0,
                          "qdistro.test.identity", {}, delegated=False)
    _as_arbitrary_admin(broker)

    with pytest.raises(dbus.DBusException) as ei:
        broker.DecideRequest(rid, "allow", "once")

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


def test_arbitrary_uid_1000_cannot_save_rule(broker):
    _as_arbitrary_admin(broker)

    with pytest.raises(dbus.DBusException) as ei:
        broker.SaveRule(
            "bad.yaml",
            "- name: bad\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.test.identity'\n",
        )

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


@pytest.mark.parametrize("method,args", [
    (
        "CheckClipboardTransfer",
        ("user1", "user1", ["text/plain"], "", "", "", True),
    ),
    (
        "CheckClipboardReceive",
        ("user1", "user1", "text/plain", "", "", "", True),
    ),
    (
        "CheckHandoffActivation",
        ("user1", "user1", "src.app", "dst.app", "", True),
    ),
])
def test_arbitrary_uid_1000_cannot_assert_identity_verified_gates(
        broker, method, args):
    _as_arbitrary_admin(broker)

    with pytest.raises(dbus.DBusException) as ei:
        getattr(broker, method)(*args)

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"
