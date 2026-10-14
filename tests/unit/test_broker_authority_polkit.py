"""Admin-uid authority calls need a password-backed polkit authorization.

The /proc identity checks in front of the broker's authority methods are
defence in depth only: a same-uid process can rewrite its own argv to look
like the installed admin app (sol bsa r4). An admin-uid peer that passes
them must also hold org.qdistro.broker.approve — auth_admin_keep, forced
onto the polkit agent's PAM route — asked non-interactively for the calling
bus name. Root maintenance paths and the in-process workflow sender are not
polkit subjects and are never asked.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("dbus")
import dbus
import qdistro_admin_broker as B
from test_broker_check_permission import (
    ADMIN_UID,
    NON_ADMIN_UID,
    PEER_EXE,
    _StubBroker,
)

_ROOT = Path(__file__).resolve().parents[2]
SENDER = ":1.4242"
INSTALLED_ARGV = ["/usr/bin/python3", "-I",
                  "/usr/local/bin/qdistro-admin-approval-app"]
GOOD_RULE = ("- name: good\n"
             "  decision: allow\n"
             "  match:\n"
             "    action: 'qdistro.test.polkit'\n")


class _PolkitStub:
    """Records every polkit question; answers `verdict` (or raises it)."""

    def __init__(self, verdict):
        self.verdict = verdict
        self.calls: list[tuple[object, str]] = []

    def __call__(self, sender, action):
        self.calls.append((sender, action))
        if isinstance(self.verdict, BaseException):
            raise self.verdict
        return self.verdict


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


def _polkit(broker, verdict) -> _PolkitStub:
    stub = _PolkitStub(verdict)
    broker._polkit_peer_authorized = stub
    return stub


def _as_installed_app(broker, monkeypatch, argv=INSTALLED_ARGV) -> None:
    """A peer that passes every /proc identity check for authority."""
    broker.set_peer(uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: list(argv))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")
    monkeypatch.setattr(B, "_read_proc_environ",
                        lambda _pid: {"HOME": "/home/admin",
                                      "PATH": "/usr/bin:/bin"})


def _pending(broker) -> int:
    return broker._enqueue(ADMIN_UID, 0, PEER_EXE, 0, "qsu.exec:root", {},
                           delegated=True)


def _rule_file(rules_dir: Path) -> Path:
    p = rules_dir / "victim.yaml"
    p.write_text(GOOD_RULE)
    return p


# Every authority method, as (name, call(broker, rules_dir, rid)).
_AUTHORITY_CALLS = {
    "DecideRequest": lambda b, _d, rid: b.DecideRequest(
        rid, "allow", "once", sender=SENDER),
    "SaveRule": lambda b, _d, _r: b.SaveRule(
        "good.yaml", GOOD_RULE, sender=SENDER),
    "DeleteRule": lambda b, d, _r: b.DeleteRule(
        str(_rule_file(d)), "good", sender=SENDER),
    "ReloadRules": lambda b, _d, _r: b.ReloadRules(sender=SENDER),
    "RevokeApproval": lambda b, _d, _r: b.RevokeApproval(1, sender=SENDER),
    "RevokeAllForUid": lambda b, _d, _r: b.RevokeAllForUid(
        NON_ADMIN_UID, sender=SENDER),
    "ApproveWorkflowRun": lambda b, _d, _r: b.ApproveWorkflowRun(
        "run-1", "0" * 64, sender=SENDER),
    "RunAuditGc": lambda b, _d, _r: b.RunAuditGc(30, sender=SENDER),
}


def test_every_authority_method_is_covered():
    assert set(_AUTHORITY_CALLS) == set(B._ADMIN_CONTROL_AUTHORITY_METHODS)


@pytest.mark.cheat_aware(
    protects="an admin-session process cannot approve its own root request "
             "without the admin's password",
    severity="critical",
    cheats=["skip the polkit check when /proc identity passes",
            "treat a polkit 'no' as a pass"],
    consequence="qsu id; busctl DecideRequest <id> allow once from any "
                "admin process forging the app's argv gets root",
)
def test_unauthorized_decide_is_auth_required_and_decides_nothing(
        broker, monkeypatch):
    rid = _pending(broker)
    _as_installed_app(broker, monkeypatch)
    pk = _polkit(broker, False)
    with pytest.raises(dbus.DBusException) as ei:
        broker.DecideRequest(rid, "allow", "once", sender=SENDER)
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AuthRequired"
    assert pk.calls == [(SENDER, B.BROKER_APPROVE_ACTION)]
    assert broker._pending[rid].decision is None
    assert broker.decided_signals == []


def test_authorized_decide_is_applied(broker, monkeypatch):
    rid = _pending(broker)
    _as_installed_app(broker, monkeypatch)
    pk = _polkit(broker, True)
    assert broker.DecideRequest(rid, "allow", "once",
                                sender=SENDER) == "applied"
    assert broker._pending[rid].decision is True
    assert pk.calls == [(SENDER, "org.qdistro.broker.approve")]


@pytest.mark.parametrize("err", [
    dbus.DBusException("no polkitd",
                       name="org.freedesktop.DBus.Error.ServiceUnknown"),
    dbus.DBusException("timeout", name="org.freedesktop.DBus.Error.NoReply"),
    RuntimeError("anything else"),
])
def test_polkit_failure_fails_closed(broker, monkeypatch, err):
    rid = _pending(broker)
    _as_installed_app(broker, monkeypatch)
    _polkit(broker, err)
    with pytest.raises(dbus.DBusException) as ei:
        broker.DecideRequest(rid, "allow", "once", sender=SENDER)
    # AccessDenied, not AuthRequired: authenticating cannot fix a broken
    # polkit, so the client must not be sent into an interactive retry.
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AccessDenied"
    assert broker._pending[rid].decision is None


@pytest.mark.parametrize("method", sorted(_AUTHORITY_CALLS))
def test_every_authority_method_asks_polkit(broker, monkeypatch, rules_dir,
                                            method):
    rid = _pending(broker)
    _as_installed_app(broker, monkeypatch)
    pk = _polkit(broker, False)
    with pytest.raises(dbus.DBusException) as ei:
        _AUTHORITY_CALLS[method](broker, rules_dir, rid)
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AuthRequired"
    assert pk.calls == [(SENDER, B.BROKER_APPROVE_ACTION)]
    assert broker._pending[rid].decision is None
    if method == "DeleteRule":
        assert (rules_dir / "victim.yaml").exists()
    if method == "SaveRule":
        assert not (rules_dir / "good.yaml").exists()


def test_delete_rule_polkit_refusal_is_audited(broker, monkeypatch,
                                               rules_dir):
    _as_installed_app(broker, monkeypatch)
    _polkit(broker, False)
    with pytest.raises(dbus.DBusException):
        broker.DeleteRule(str(_rule_file(rules_dir)), "good", sender=SENDER)
    rows = [r for r in broker.audit.recent(limit=50)
            if r["action"] == "qdistro.rules.delete"]
    assert rows and bool(rows[0]["decision"]) is False
    assert "AuthRequired" in str(rows[0]["source"])
    assert (rules_dir / "victim.yaml").exists()


@pytest.mark.cheat_aware(
    protects="argv forgery alone grants no approval authority",
    severity="critical",
    cheats=["rely on /proc cmdline to identify the installed app"],
    consequence="python3 -I -c CODE rewriting its argv buffer approves its "
                "own root request (sol bsa r4)",
)
def test_forged_argv_passes_identity_but_still_needs_polkit(broker,
                                                            monkeypatch):
    """sol bsa r4: `/usr/bin/python3 -I -c CODE` rewrites its own argv
    memory to read `/usr/bin/python3 -I <installed app>`. The identity
    layer cannot tell — the polkit layer still refuses."""
    rid = _pending(broker)
    _as_installed_app(broker, monkeypatch)
    ok, reason = broker._peer_matches_admin_control(
        uid=ADMIN_UID, pid=1234, exe="/usr/bin/python3",
        method="DecideRequest")
    assert ok, reason          # the forgery fools the identity layer ...
    _polkit(broker, False)
    with pytest.raises(dbus.DBusException) as ei:
        broker.DecideRequest(rid, "allow", "once", sender=SENDER)
    assert ei.value.get_dbus_name() == B.BUS_NAME + ".AuthRequired"
    assert broker._pending[rid].decision is None  # ... but not polkit


@pytest.mark.parametrize("python_flag", ["-", "-c"])
def test_root_maintenance_path_never_asks_polkit(broker, monkeypatch,
                                                 python_flag):
    rid = _pending(broker)
    broker.set_peer(uid=0, pid=1234, exe="/usr/bin/python3")
    monkeypatch.setattr(B, "_read_proc_cmdline",
                        lambda _pid: ["python3", python_flag])
    pk = _polkit(broker, False)
    assert broker.DecideRequest(rid, "allow", "once",
                                sender=SENDER) == "applied"
    assert str(broker.SaveRule("good.yaml", GOOD_RULE,
                               sender=SENDER)).endswith("/good.yaml")
    assert pk.calls == []


def test_root_cli_delete_rule_never_asks_polkit(broker, monkeypatch,
                                                rules_dir):
    broker.set_peer(uid=0, pid=1234, exe="/usr/local/sbin/qdistro-approvals")
    monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: [])
    pk = _polkit(broker, False)
    deleted, _n, _errs = broker.DeleteRule(str(_rule_file(rules_dir)),
                                           "good", sender=SENDER)
    assert deleted and pk.calls == []


@pytest.mark.parametrize("call", [
    lambda b: b.GetPending(sender=SENDER),
    lambda b: b.ListRules(sender=SENDER),
    lambda b: b.ListHistory(10, sender=SENDER),
    lambda b: b.ListCache(sender=SENDER),
])
def test_read_methods_never_ask_polkit(broker, monkeypatch, call):
    _as_installed_app(broker, monkeypatch)
    pk = _polkit(broker, False)
    call(broker)
    assert pk.calls == []


def test_workflow_sender_never_asks_polkit(broker, monkeypatch):
    """The in-process workflow proxy is no bus peer: its own whitelist
    gates it, polkit is never consulted."""
    pk = _polkit(broker, False)
    monkeypatch.setattr(B, "_read_proc_identity",
                        lambda _pid: ("/usr/bin/python3", 1))
    broker._peer_info = lambda sender, conn: (0, os.getpid(),
                                              "/usr/bin/python3", 1)
    broker._require_authority_authorization(0, B._WORKFLOW_SENDER,
                                            "DecideRequest")
    broker._require_authority_authorization(ADMIN_UID, B._WORKFLOW_SENDER,
                                            "DecideRequest")
    assert pk.calls == []


def test_polkit_question_is_non_interactive_for_the_bus_name(monkeypatch):
    """The broker asks for the CALLING bus name with flags 0 (never
    AllowUserInteraction — it must not wait on a human) and a bounded
    timeout."""
    seen = {}

    class _Iface:
        def __init__(self, obj, iface):
            seen["obj"], seen["iface"] = obj, iface

        def CheckAuthorization(self, subject, action, details, flags,
                               cancel_id, timeout=None):
            seen.update(subject=subject, action=action, details=details,
                        flags=flags, cancel_id=cancel_id, timeout=timeout)
            return (dbus.Boolean(True), dbus.Boolean(False), {})

    class _Bus:
        def get_object(self, bus, path):
            return (bus, path)

    monkeypatch.setattr(B.dbus, "SystemBus", lambda: _Bus())
    monkeypatch.setattr(B.dbus, "Interface", _Iface)
    assert B.Broker._polkit_peer_authorized(None, SENDER,
                                            B.BROKER_APPROVE_ACTION) is True
    assert seen["obj"] == (B.POLKIT_BUS, B.POLKIT_AUTHORITY_OBJ)
    assert seen["iface"] == B.POLKIT_AUTHORITY_IFACE
    kind, fields = seen["subject"]
    assert kind == "system-bus-name" and dict(fields) == {"name": SENDER}
    assert seen["action"] == "org.qdistro.broker.approve"
    assert dict(seen["details"]) == {}
    assert int(seen["flags"]) == 0
    assert 0 < seen["timeout"] <= 10


# -- shipped policy / install -------------------------------------------


def test_policy_ships_password_only_defaults():
    import xml.etree.ElementTree as ET
    root = ET.parse(_ROOT / "broker/org.qdistro.broker.policy").getroot()
    actions = {a.get("id"): a for a in root.iter("action")}
    assert set(actions) == {B.BROKER_APPROVE_ACTION}
    d = actions[B.BROKER_APPROVE_ACTION].find("defaults")
    assert d.findtext("allow_any") == "no"
    assert d.findtext("allow_inactive") == "no"
    assert d.findtext("allow_active") == "auth_admin_keep"
    assert "approve" in actions[B.BROKER_APPROVE_ACTION].findtext(
        "description").lower()


def test_installer_and_image_verify_ship_the_policy():
    inst = (_ROOT / "scripts/install/install-broker-for-qdwin.sh").read_text()
    assert re.search(r'install -m 0644 .*"\$BROKER_SRC/org\.qdistro\.broker'
                     r'\.policy"', inst)
    assert "/usr/share/polkit-1/actions/org.qdistro.broker.policy" in inst
    verify = (_ROOT / "image/verify-contents.sh").read_text()
    assert re.search(r"check_req .*/usr/share/polkit-1/actions/"
                     r"org\.qdistro\.broker\.policy", verify)
    assert re.search(r"check_absent .*10-qci-broker-approve-test\.rules",
                     verify)


def test_selinux_module_lets_the_broker_ask_polkit():
    te = (_ROOT / "selinux/broker/qdistro_broker.te").read_text()
    assert re.search(r"policykit_dbus_chat\(qdistro_broker_t\)", te)


def test_agent_forces_pam_for_the_action():
    import qdistro_polkit_agent as A
    assert B.BROKER_APPROVE_ACTION in A.FORCED_PAM_ACTIONS

