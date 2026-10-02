"""Privileged broker methods require trusted peer identity, not uid alone."""
from __future__ import annotations

from pathlib import Path
import shlex

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


def _installed_admin_app_argv() -> list[str]:
    """Read the production command and ensure the installer lays it down.

    The broker sees the Python process argv, not the desktop wrapper path.
    Deriving that argv from the shipped launcher prevents an independent test
    fixture from silently retaining the broker's old trusted script path.
    """
    root = Path(__file__).resolve().parents[2]
    launcher = root / "deploy/start-admin-app-wayland.sh"
    installer = root / "scripts/install/install-admin-app-for-vm.sh"
    lines = [line for line in launcher.read_text().splitlines()
             if line.startswith("exec /usr/bin/python3 ")]
    assert len(lines) == 1, f"expected one Python exec in {launcher}: {lines}"
    argv = shlex.split(lines[0])
    assert len(argv) == 4 and argv[0] == "exec" and argv[-1] == "$@", argv
    assert f'"$DESTDIR{argv[2]}"' in installer.read_text(), (
        f"installer does not install the launched script {argv[2]}")
    return argv[1:3]


@pytest.mark.parametrize("decision,allowed", [("allow", True), ("deny", False)])
def test_installed_admin_app_can_read_and_decide_pending(
        broker, monkeypatch, decision, allowed):
    argv = _installed_admin_app_argv()
    rid = broker._enqueue(NON_ADMIN_UID, 0, PEER_EXE, 0,
                          "qdistro.test.installed-admin-app", {},
                          delegated=False, one_shot=True)
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe=argv[0])
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: argv)
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")

    pending = broker.GetPending()
    assert [int(row["id"]) for row in pending] == [rid]
    assert str(pending[0]["action"]) == "qdistro.test.installed-admin-app"
    broker.DecideRequest(rid, decision, "once")
    assert broker._pending[rid].decision is allowed
    assert broker.decided_signals[-1] == (rid, decision)
    assert broker.GetPending() == []


@pytest.mark.cheat_aware(
    protects="Installed app identity must not admit unrelated or non-admin peers",
    severity="critical",
    cheats=["trust any uid-1000 Python script", "skip the broker peer check"],
    consequence="A different process could approve a request without the admin UI",
)
def test_installed_admin_app_identity_does_not_trust_other_peers(
        broker, monkeypatch):
    trusted_argv = _installed_admin_app_argv()
    rid = broker._enqueue(NON_ADMIN_UID, 0, PEER_EXE, 0,
                          "qdistro.test.installed-admin-app", {},
                          delegated=False, one_shot=True)
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    # An arbitrary admin Python process and a non-admin process claiming the
    # approved script argv must both be refused by the real broker methods.
    for uid, argv in ((ADMIN_UID, [trusted_argv[0], "/tmp/other.py"]),
                      (NON_ADMIN_UID, trusted_argv)):
        broker.set_peer(uid=uid, pid=1234, exe=trusted_argv[0])
        monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid, a=argv: a)
        for method, args in ((broker.GetPending, ()),
                             (broker.DecideRequest, (rid, "allow", "once"))):
            with pytest.raises(dbus.DBusException) as ei:
                method(*args)
            assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"
    assert broker._pending[rid].decision is None


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


def test_pytest_env_does_not_trust_synthetic_admin_exes(broker, monkeypatch):
    """iso2 01 F3: PYTEST_CURRENT_TEST must not widen the admin-control
    allowlist. Tests inject trust by overriding the predicate, not via
    the environment."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/unit/test_x.py::t")
    broker.set_peer(uid=ADMIN_UID, pid=DEAD_PID, exe="/usr/bin/test-app")
    ok, reason = broker._peer_matches_admin_control(
        uid=ADMIN_UID, pid=DEAD_PID, exe="/usr/bin/test-app",
        method="DecideRequest",
    )
    assert not ok
    assert "untrusted" in reason or "not an installed" in reason or "not trusted" in reason


def test_admin_python_c_can_revoke_all_for_uid(broker, monkeypatch):
    """RevokeAllForUid is an admin-control method the s57 probe drives
    via `python3 -c`. It must be in _ADMIN_CONTROL_STDIN_METHODS so a
    live admin interpreter is trusted the same way as DecideRequest.
    """
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda _pid: ("/usr/bin/python3", 1))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["python3", "-c", "iface.RevokeAllForUid(1)"])

    ok, reason = broker._peer_matches_admin_control(
        uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3",
        method="RevokeAllForUid",
    )
    assert ok, reason
    assert "RevokeAllForUid" in B._ADMIN_CONTROL_STDIN_METHODS
