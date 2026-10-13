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
    assert len(argv) == 5 and argv[0] == "exec" and argv[-1] == "$@", argv
    assert argv[2] == "-I", f"admin app must run isolated: {argv}"
    assert f'"$DESTDIR{argv[3]}"' in installer.read_text(), (
        f"installer does not install the launched script {argv[3]}")
    return argv[1:4]


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
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: {})

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
def test_live_admin_python_cannot_save_rule(broker, monkeypatch, python_flag):
    """Inline admin Python is any admin-session process: it may not write
    an allow rule (authority method)."""
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["python3", python_flag])
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda _pid: ("/usr/bin/python3", 1))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")

    with pytest.raises(dbus.DBusException) as ei:
        broker.SaveRule(
            "good.yaml",
            "- name: good\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.test.identity'\n",
        )
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


@pytest.mark.parametrize("python_flag", ["-", "-c"])
def test_root_inline_python_can_save_rule(broker, monkeypatch, python_flag):
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["python3", python_flag])

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


def test_admin_dbus_client_cannot_reload_rules_even_when_method_named(
        broker, monkeypatch):
    """ReloadRules is an authority method: an admin-uid busctl is any
    admin-session process (fable 2026-10-13)."""
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/busctl")
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
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

    with pytest.raises(dbus.DBusException) as ei:
        broker.ReloadRules()
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"


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


def test_admin_python_c_cannot_revoke_all_for_uid(broker, monkeypatch):
    """RevokeAllForUid is an authority method: inline admin Python is
    refused (root inline Python keeps the s57 probe path)."""
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
    assert not ok, reason
    ok, reason = broker._peer_matches_admin_control(
        uid=0, pid=1234, exe="/usr/bin/python3", method="RevokeAllForUid")
    assert ok, reason


def _bus_argv(method):
    return ["busctl", "--system", "call", B.BUS_NAME, B.OBJ_PATH,
            B.BUS_NAME, method]


@pytest.mark.cheat_aware(
    protects="An admin-session process cannot approve its own qsu root request",
    severity="critical",
    cheats=["trust admin-uid busctl naming the method",
            "trust inline admin python for authority methods"],
    consequence="any admin-uid process gets root with no prompt: qsu id; "
                "busctl GetPending; busctl DecideRequest <id> allow once",
)
@pytest.mark.parametrize("exe,argv", [
    ("/usr/bin/busctl", _bus_argv("DecideRequest")),
    ("/usr/bin/dbus-send", ["dbus-send", "--system", f"--dest={B.BUS_NAME}",
                            B.OBJ_PATH, f"{B.BUS_NAME}.DecideRequest"]),
    ("/usr/bin/gdbus", ["gdbus", "call", "--system", "--dest", B.BUS_NAME,
                        "--object-path", B.OBJ_PATH, "--method",
                        f"{B.BUS_NAME}.DecideRequest"]),
    ("/usr/bin/python3", ["python3", "-c", "o.DecideRequest(1,'allow','once')"]),
    ("/usr/bin/python3", ["python3", "-"]),
    # An installed path merely present in argv is not that program.
    ("/usr/bin/python3", ["python3", "-c", "CODE",
                          "/usr/local/bin/qdistro-admin-approval-app"]),
    # Not isolated: user-site .pth/usercustomize run inside the app.
    ("/usr/bin/python3", ["python3", "/usr/local/bin/qdistro-admin-approval-app"]),
    ("/usr/bin/python3", ["python3", "-s", "/usr/local/bin/qdistro-admin-tui"]),
    # An admin-writable dev copy never exercises authority.
    ("/usr/bin/python3", ["python3",
                          "/home/admin/qdistro/admin_app/qdistro_admin_app.py"]),
])
def test_admin_session_process_cannot_self_approve(broker, monkeypatch, exe,
                                                    argv):
    rid = broker._enqueue(ADMIN_UID, 0, PEER_EXE, 0, "qsu.exec:root", {},
                          delegated=True)
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe=exe)
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: argv)
    monkeypatch.setattr(B, "_read_proc_identity", lambda _pid: (exe, 1))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: {})

    with pytest.raises(dbus.DBusException) as ei:
        broker.DecideRequest(rid, "allow", "once")
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"
    assert broker._pending[rid].decision is None


def test_admin_busctl_may_still_read_pending(broker, monkeypatch):
    """Read-only control methods keep the admin-uid D-Bus CLI path."""
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/busctl")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: _bus_argv("GetPending"))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    assert broker.GetPending() == []


@pytest.mark.parametrize("env", [
    {"LD_PRELOAD": "/home/admin/x.so"}, {"PYTHONPATH": "/home/admin"},
    {"PYTHONSTARTUP": "/home/admin/s.py"},
    # Toolkit loaders -I does not cover (sol bsa r2): an allowlist, so any
    # unlisted name refuses, not only these.
    {"QT_PLUGIN_PATH": "/home/admin/plugins"},
    {"QT_QPA_PLATFORM_PLUGIN_PATH": "/home/admin/plugins"},
    {"QML2_IMPORT_PATH": "/home/admin/qml"},
    {"GTK_MODULES": "evil"}, {"GIO_EXTRA_MODULES": "/home/admin/gio"},
    {"GCONV_PATH": "/home/admin/gconv"}, {"OPENSSL_CONF": "/home/admin/c"},
    {"SOME_FUTURE_LOADER_PATH": "/home/admin"},
    {"QT_QPA_PLATFORM": "xcb"},
    {"QT_QPA_PLATFORM": "wayland:/home/admin"},
])
def test_installed_admin_app_with_injection_env_cannot_decide(
        broker, monkeypatch, env):
    argv = _installed_admin_app_argv()
    rid = broker._enqueue(NON_ADMIN_UID, 0, PEER_EXE, 0,
                          "qdistro.test.installed-admin-app", {},
                          delegated=False, one_shot=True)
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe=argv[0])
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: argv)
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: env)
    with pytest.raises(dbus.DBusException):
        broker.DecideRequest(rid, "allow", "once")
    assert broker._pending[rid].decision is None


def test_installed_admin_app_with_isolated_flag_can_decide(broker,
                                                            monkeypatch):
    rid = broker._enqueue(NON_ADMIN_UID, 0, PEER_EXE, 0,
                          "qdistro.test.installed-admin-app", {},
                          delegated=False, one_shot=True)
    argv = ["/usr/bin/python3", "-I", "/usr/local/bin/qdistro-admin-tui"]
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: argv)
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: {})
    assert broker.DecideRequest(rid, "allow", "once") == "applied"


@pytest.mark.parametrize("method,args", [
    ("ApproveWorkflowRun", ("run-1", "0" * 64)),
    ("RunAuditGc", (30,)),
])
def test_admin_cli_cannot_call_other_authority_methods(broker, monkeypatch,
                                                       method, args):
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/busctl")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: _bus_argv(method))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    ok, reason = broker._peer_matches_admin_control(
        uid=ADMIN_UID, pid=1234, exe="/usr/bin/busctl", method=method)
    assert not ok, reason


def test_qdshell_is_not_an_authority_caller(broker, monkeypatch):
    """qdshell (qs -p profile) reads control-plane state but calls no
    authority method; it is not admitted to one."""
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/qs")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["/usr/bin/qs", "-p", B._QDSHELL_PROFILE])
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    ok, _ = broker._peer_matches_admin_control(
        uid=ADMIN_UID, pid=1234, exe="/usr/bin/qs", method="DecideRequest")
    assert not ok


@pytest.mark.parametrize("extra", [
    ["-platformpluginpath", "/home/admin/plugins", "-platform", "evil"],
    ["anything"],
])
def test_installed_admin_app_with_arguments_cannot_decide(broker, monkeypatch,
                                                          extra):
    """Qt parses -platformpluginpath from argv; the trusted shape takes no
    arguments at all (sol bsa r2)."""
    rid = broker._enqueue(NON_ADMIN_UID, 0, PEER_EXE, 0,
                          "qdistro.test.installed-admin-app", {},
                          delegated=False, one_shot=True)
    argv = _installed_admin_app_argv() + extra
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe=argv[0])
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: argv)
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: {})
    with pytest.raises(dbus.DBusException):
        broker.DecideRequest(rid, "allow", "once")
    assert broker._pending[rid].decision is None


_ROOT = Path(__file__).resolve().parents[2]
_AUTHORITY_SCRIPTS = {
    "/usr/local/bin/qdistro-admin-approval-app":
        _ROOT / "admin_app/qdistro_admin_app.py",
    "/usr/local/bin/qdistro-admin-tui": _ROOT / "tui/qdistro_admin_tui.py",
}
_REEXEC_NAMES = {"_INSTALLED_PATH", "_AUTHORITY_ENV_ALLOW",
                 "_AUTHORITY_QPA_VALUES", "_authority_env",
                 "_reexec_for_authority"}


def _load_reexec(src: Path, argv, environ, isolated):
    """Run the script's re-exec helper alone (the module imports
    Qt/Textual) against a fake os/sys; returns (namespace, execve calls)."""
    import ast
    import types
    tree = ast.parse(src.read_text())
    keep = []
    for node in tree.body:
        names = ({t.id for t in node.targets if isinstance(t, ast.Name)}
                 if isinstance(node, ast.Assign)
                 else {getattr(node, "name", None)})
        if names & _REEXEC_NAMES:
            keep.append(node)
    assert {getattr(n, "name", None) or n.targets[0].id
            for n in keep} == _REEXEC_NAMES, src
    calls = []
    fake_os = types.SimpleNamespace(
        environ=dict(environ),
        execve=lambda path, args, env: calls.append((path, args, env)))
    fake_sys = types.SimpleNamespace(
        argv=list(argv), flags=types.SimpleNamespace(isolated=isolated))
    ns = {"os": fake_os, "sys": fake_sys}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(src),  # noqa: S102
                 "exec"), ns)
    ns["_reexec_for_authority"]()
    return ns, calls


@pytest.mark.parametrize("installed", sorted(_AUTHORITY_SCRIPTS))
def test_authority_scripts_share_the_broker_env_allowlist(installed):
    ns, _ = _load_reexec(_AUTHORITY_SCRIPTS[installed], [installed], {}, 1)
    assert ns["_INSTALLED_PATH"] == installed
    assert installed in B._ADMIN_CONTROL_INSTALLED_SCRIPT_PATHS
    assert ns["_AUTHORITY_ENV_ALLOW"] == B._ADMIN_CONTROL_ENV_ALLOW
    assert ns["_AUTHORITY_QPA_VALUES"] == B._ADMIN_CONTROL_QPA_VALUES


@pytest.mark.parametrize("installed", sorted(_AUTHORITY_SCRIPTS))
def test_installed_script_reexecs_into_a_trusted_shape(broker, monkeypatch,
                                                       installed):
    """A launch with session noise, plugin paths and Qt arguments re-execs
    into exactly the shape the broker grants authority to."""
    dirty = {"HOME": "/home/admin", "WAYLAND_DISPLAY": "wayland-1",
             "XDG_RUNTIME_DIR": "/run/user/1000", "LC_ALL": "C.UTF-8",
             "QT_PLUGIN_PATH": "/home/admin/plugins", "GTK_MODULES": "evil",
             "QT_QPA_PLATFORM": "xcb", "PATH": "/home/admin/bin:/usr/bin"}
    _, calls = _load_reexec(
        _AUTHORITY_SCRIPTS[installed],
        [installed, "-platformpluginpath", "/home/admin/plugins"], dirty, 0)
    assert len(calls) == 1
    path, argv, env = calls[0]
    assert path == argv[0] == "/usr/bin/python3"
    assert argv == ["/usr/bin/python3", "-I", installed]
    assert env == {"HOME": "/home/admin", "WAYLAND_DISPLAY": "wayland-1",
                   "XDG_RUNTIME_DIR": "/run/user/1000", "LC_ALL": "C.UTF-8",
                   "PATH": "/usr/bin:/bin"}
    # The re-exec'd process is a fixed point (no loop) ...
    _, again = _load_reexec(_AUTHORITY_SCRIPTS[installed], [installed],
                            env, 1)
    assert again == []
    # ... and the broker grants it authority.
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: env)
    ok, reason = broker._peer_is_installed_admin_script(
        1234, "/usr/bin/python3", argv)
    assert ok, reason


def test_dev_copy_is_not_reexecd():
    src = _AUTHORITY_SCRIPTS["/usr/local/bin/qdistro-admin-approval-app"]
    _, calls = _load_reexec(src, [str(src)], {"QT_PLUGIN_PATH": "/x"}, 0)
    assert calls == []


@pytest.mark.parametrize("exe", ["/home/admin/python3", "/tmp/python3.13",
                                 "/usr/bin/python3 (deleted)"])
def test_admin_owned_interpreter_named_python_cannot_decide(broker,
                                                            monkeypatch, exe):
    """The authority predicate binds the system interpreter, not a
    basename: a native client named python3 ignores argv and env (sol bsa
    r3)."""
    rid = broker._enqueue(NON_ADMIN_UID, 0, PEER_EXE, 0,
                          "qdistro.test.installed-admin-app", {},
                          delegated=False, one_shot=True)
    argv = [exe, "-I", "/usr/local/bin/qdistro-admin-approval-app"]
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe=exe)
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: argv)
    monkeypatch.setattr(B, "_read_proc_identity", lambda _pid: (exe, 1))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_environ", lambda _pid: {})
    with pytest.raises(dbus.DBusException):
        broker.DecideRequest(rid, "allow", "once")
    assert broker._pending[rid].decision is None
