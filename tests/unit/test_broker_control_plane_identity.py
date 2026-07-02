"""Privileged broker methods require trusted peer identity, not uid alone."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("dbus")
import dbus  # noqa: E402
import qdistro_admin_broker as B  # noqa: E402
from test_broker_check_permission import (  # noqa: E402
    ADMIN_UID,
    NON_ADMIN_UID,
    PEER_EXE,
    _StubBroker,
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


@pytest.mark.parametrize("python_flag", ["-", "-c"])
def test_live_admin_python_can_save_rule(broker, monkeypatch, python_flag):
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["python3", python_flag])
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda _pid: ("/usr/bin/python3", 1))

    path = broker.SaveRule(
        "good.yaml",
        "- name: good\n"
        "  decision: allow\n"
        "  match:\n"
        "    action: 'qdistro.test.identity'\n",
    )

    assert str(path).endswith("/good.yaml")


def test_root_dbus_client_must_name_broker_method(broker, monkeypatch):
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            "--print-reply",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.ReloadRules",
        ],
    )

    assert broker.ReloadRules()[0] >= 0


def test_root_dbus_client_wrong_method_is_denied(broker, monkeypatch):
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.GetPending",
        ],
    )

    with pytest.raises(dbus.DBusException) as ei:
        broker.ReloadRules()

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


def test_admin_dbus_client_can_reload_rules_when_method_named(
        broker, monkeypatch):
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/busctl")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "busctl",
            "--system",
            "call",
            B.BUS_NAME,
            B.OBJ_PATH,
            B.BUS_NAME,
            "ReloadRules",
        ],
    )

    assert broker.ReloadRules()[0] >= 0


def test_admin_dbus_client_control_wrong_method_is_denied(
        broker, monkeypatch):
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/busctl")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "busctl",
            "--system",
            "call",
            B.BUS_NAME,
            B.OBJ_PATH,
            B.BUS_NAME,
            "GetPending",
        ],
    )

    with pytest.raises(dbus.DBusException) as ei:
        broker.ReloadRules()

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


def test_admin_dbus_client_can_probe_qdshell_gate_when_method_named(
        broker, monkeypatch):
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.CheckClipboardTransfer",
        ],
    )

    assert broker.CheckClipboardTransfer(
        "user1", "admin", ["text/plain"],
        "src.app", "dst.app", "qdistro.tier3",
    ) == "deny"


def test_admin_dbus_client_wrong_qdshell_gate_method_is_denied(
        broker, monkeypatch):
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.CheckClipboardReceive",
        ],
    )

    with pytest.raises(dbus.DBusException) as ei:
        broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "src.app", "dst.app", "qdistro.tier3",
        )

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


def test_root_dbus_client_can_probe_qdshell_gate_when_method_named(
        broker, monkeypatch):
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.CheckClipboardTransfer",
        ],
    )

    assert broker.CheckClipboardTransfer(
        "user1", "admin", ["text/plain"],
        "src.app", "dst.app", "qdistro.tier3",
    ) == "deny"


def test_root_dbus_client_qdshell_gate_wrong_method_is_denied(
        broker, monkeypatch):
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.CheckClipboardReceive",
        ],
    )

    with pytest.raises(dbus.DBusException) as ei:
        broker.CheckClipboardTransfer(
            "user1", "admin", ["text/plain"],
            "src.app", "dst.app", "qdistro.tier3",
        )

    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


def test_root_lineage_dbus_client_must_name_broker_method(broker, monkeypatch):
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/dbus-send")
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda _pid: ("/usr/bin/dbus-send", 1))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(
        B,
        "_read_proc_cmdline",
        lambda _pid: [
            "dbus-send",
            "--system",
            f"--dest={B.BUS_NAME}",
            B.OBJ_PATH,
            f"{B.BUS_NAME}.GetLineageReceiptContext",
        ],
    )

    ok, reason = broker._peer_matches_root_helper(
        pid=1234,
        exe="/usr/bin/dbus-send",
        family="lineage",
        method="GetLineageReceiptContext",
    )

    assert ok, reason


@pytest.mark.parametrize("python_flag", ["-", "-c"])
@pytest.mark.parametrize("family", ["lineage", "qsu"])
def test_root_python_control_script_must_be_live(
        broker, monkeypatch, python_flag, family):
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda _pid: ("/usr/bin/python3", 1))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["python3", python_flag])

    ok, reason = broker._peer_matches_root_helper(
        pid=1234,
        exe="/usr/bin/python3",
        family=family,
        method=("GetLineageReceiptContext" if family == "lineage"
                else "RequestPermissionAs"),
    )

    assert ok, reason


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
