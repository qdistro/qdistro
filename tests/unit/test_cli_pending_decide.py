"""qdistro-approvals pending / approve / deny.

Two layers:

* A fake bus (``_FakeBroker``) that mirrors the broker's GetPending /
  DecideRequest contract, for output shape, exit codes and the CLI's own
  validation (unknown id, scope choices, hostile request text).
* The REAL broker methods (``_StubBroker`` from the broker unit tests) behind
  a thin adapter, with the peer identity the kernel gives the installed CLI:
  uid 0, /proc/<pid>/exe = the interpreter, argv = [shebang interpreter,
  installed path, ...]. That proves the broker's control-plane check admits
  the CLI as shipped (and refuses it run from a source tree), and that a
  decision reaches the requester's waiter and the audit log.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

import qdistro_approvals as cli

_REPO = Path(__file__).resolve().parents[2]


def _run(argv, capsys):
    old_argv = sys.argv
    sys.argv = ["qdistro-approvals"] + argv
    try:
        rc = cli.main()
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = old_argv
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture(autouse=True)
def _as_root(monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)


# --------------------------------------------------------------------------
# Layer 1: fake bus
# --------------------------------------------------------------------------

class _FakeDBusException(Exception):
    def __init__(self, msg="", name=""):
        super().__init__(msg)
        self._name = name
        self._msg = msg

    def get_dbus_name(self):
        return self._name

    def get_dbus_message(self):
        return self._msg


class _FakeDbusModule:
    DBusException = _FakeDBusException


class _FakeBroker:
    """GetPending/DecideRequest with the broker's observable semantics:
    unknown/decided ids are a silent no-op, bad scope is BadArgument, and a
    decided request leaves GetPending."""

    def __init__(self):
        self.pending: dict[int, dict] = {}
        self.calls: list[tuple] = []
        self.ignore_decide = False
        self.history: list[dict] = []   # newest first, like ListHistory

    def add(self, rid, uid=1001, pid=4242, exe="/usr/bin/foo",
            action="qdistro.test.cli", details=None):
        self.pending[rid] = {"id": rid, "uid": uid, "pid": pid, "exe": exe,
                             "action": action, "details": details or {},
                             "exe_sha256": "", "selinux_label": "",
                             "cgroup": "", "layered_pending": False}

    def GetPending(self):
        return list(self.pending.values())

    def DecideRequest(self, rid, decision, scope):
        self.calls.append((int(rid), str(decision), str(scope)))
        if scope not in cli.SCOPES:
            raise _FakeDBusException(
                f"scope must be one of ..., got {scope!r}",
                "org.qdistro.AdminBroker1.BadArgument")
        if self.ignore_decide:
            return
        req = self.pending.pop(int(rid), None)
        if req is not None:
            self.history.insert(0, {
                "ts": int(time.time()), "request_id": req["id"],
                "caller_uid": req["uid"], "caller_pid": req["pid"],
                "action": req["action"], "decision": decision == "allow",
                "scope": scope, "source": "prompt", "approver_uid": 0})

    def ListHistory(self, limit):
        return list(self.history[:limit])


@pytest.fixture
def fake(monkeypatch):
    b = _FakeBroker()
    monkeypatch.setattr(cli, "_broker", lambda: (b, _FakeDbusModule))
    return b


def test_pending_empty(fake, capsys):
    rc, out, _ = _run(["pending"], capsys)
    assert rc == 0
    assert "no pending requests" in out


def test_pending_lists_rows_with_details(fake, capsys):
    fake.add(7, uid=1001, pid=555, exe="/usr/bin/zypper",
             action="qdistro.qsu.exec",
             details={"argv[00]": "/usr/bin/zypper", "argv[01]": "ref"})
    fake.add(3, action="qdistro.test.other")
    rc, out, _ = _run(["pending"], capsys)
    assert rc == 0
    lines = out.splitlines()
    # sorted by id, one header + rule, then rows
    first_ids = [l.split()[0] for l in lines[2:] if re.match(r"^\s+\d+\s+\d+", l)]
    assert first_ids == ["3", "7"]
    assert "qdistro.qsu.exec" in out
    assert "exe=/usr/bin/zypper" in out
    assert "argv[01]=ref" in out


def test_pending_json(fake, capsys):
    fake.add(5, details={"k": "v"})
    rc, out, _ = _run(["pending", "--json"], capsys)
    assert rc == 0
    rows = json.loads(out)
    assert rows[0]["id"] == 5 and rows[0]["details"] == {"k": "v"}


def test_pending_escapes_terminal_control_sequences(fake, capsys):
    # action/exe/details are requester-controlled: an escape sequence must
    # not reach the admin's (root) terminal raw.
    fake.add(1, action="evil\x1b]0;pwned\x07", exe="/x\x1b[2J",
             details={"a\x1b": "b\r"})
    rc, out, _ = _run(["pending"], capsys)
    assert rc == 0
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out
    assert "evil\\x1b]0;pwned\\x07" in out


def test_approve_default_scope_once(fake, capsys):
    fake.add(9, uid=1001, action="qdistro.test.cli")
    rc, out, err = _run(["approve", "9"], capsys)
    assert rc == 0, err
    assert fake.calls == [(9, "allow", "once")]
    assert "approved request id=9 uid=1001" in out
    assert "(scope: once)" in out
    assert "warning" not in err


def test_approve_with_scope_warns_about_caching(fake, capsys):
    fake.add(9)
    rc, out, err = _run(["approve", "9", "--scope", "24h"], capsys)
    assert rc == 0
    assert fake.calls == [(9, "allow", "24h")]
    assert "warning: scope '24h' caches" in err


def test_approve_rejects_unknown_scope_before_the_bus(fake, capsys):
    fake.add(9)
    rc, _out, err = _run(["approve", "9", "--scope", "always"], capsys)
    assert rc == 2
    assert "invalid choice" in err
    assert fake.calls == []


def test_cli_scopes_match_broker_valid_scopes():
    B = pytest.importorskip("qdistro_admin_broker")
    assert set(cli.SCOPES) == set(B._VALID_SCOPES)


def test_deny_sends_deny_once(fake, capsys):
    fake.add(4, action="qdistro.test.cli")
    rc, out, err = _run(["deny", "4"], capsys)
    assert rc == 0, err
    assert fake.calls == [(4, "deny", "once")]
    assert "denied request id=4" in out


@pytest.mark.parametrize("verb", ["approve", "deny"])
def test_unknown_id_is_an_error_and_never_decided(fake, capsys, verb):
    # The broker silently ignores an unknown id; the CLI must not report
    # that as success.
    fake.add(1)
    rc, _out, err = _run([verb, "99"], capsys)
    assert rc == 1
    assert "no pending request with id=99" in err
    assert fake.calls == []


def test_unconfirmed_outcome_is_not_success(fake, capsys):
    # The request may vanish from pending for reasons other than this
    # command (a concurrent decider); with no matching audit row the CLI
    # must say so and fail.
    fake.add(2)
    fake.ignore_decide = True
    rc, out, err = _run(["approve", "2"], capsys)
    assert rc == 3
    assert "outcome is unconfirmed" in err
    assert "approved" not in out


def test_unreadable_history_is_not_success(fake, capsys, monkeypatch):
    fake.add(2)

    def boom(_limit):
        raise _FakeDBusException("nope", "org.freedesktop.DBus.Error.NoReply")
    monkeypatch.setattr(fake, "ListHistory", boom)
    rc, out, err = _run(["deny", "2"], capsys)
    assert rc == 3
    assert "outcome is unconfirmed" in err and "NoReply" in err
    assert "denied" not in out


def test_stale_audit_row_from_reused_id_does_not_confirm(fake, capsys):
    # Broker ids restart at 1: an old row with the same id but another
    # caller must not count as this command's decision.
    fake.add(1, uid=1001, pid=4242)
    fake.ignore_decide = True
    fake.history.append({"ts": int(time.time()), "request_id": 1,
                         "caller_uid": 1001, "caller_pid": 99,
                         "action": "qdistro.test.cli", "decision": True,
                         "scope": "once", "source": "prompt",
                         "approver_uid": 0})
    rc, _out, err = _run(["approve", "1"], capsys)
    assert rc == 3 and "unconfirmed" in err


def test_broker_error_is_reported_with_its_name(fake, capsys, monkeypatch):
    fake.add(2)

    def boom(*_a):
        raise _FakeDBusException(
            "scope 'forever' not permitted for delegated requests",
            "org.qdistro.AdminBroker1.ScopeNotPermitted")
    monkeypatch.setattr(fake, "DecideRequest", boom)
    rc, _out, err = _run(["approve", "2", "--scope", "forever"], capsys)
    assert rc == 1
    assert "approve failed: ScopeNotPermitted:" in err


@pytest.mark.parametrize("argv", [["pending"], ["approve", "1"], ["deny", "1"]])
def test_new_subcommands_require_root(fake, capsys, monkeypatch, argv):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    fake.add(1)
    rc, _out, err = _run(argv, capsys)
    assert rc == 1
    assert "must be run as root" in err
    assert fake.calls == []


# --------------------------------------------------------------------------
# Layer 2: the real broker methods, with the installed CLI's peer identity
# --------------------------------------------------------------------------

def _installed_cli_argv(*args: str) -> list[str]:
    """argv the kernel builds when root runs the installed CLI.

    Derived from the shipped files, not restated: the CLI's shebang
    interpreter and the path the installer drops it at.
    """
    shebang = (_REPO / "cli/qdistro_approvals.py").read_text().splitlines()[0]
    assert shebang.startswith("#!")
    interp = shebang[2:].split()
    installer = (_REPO / "scripts/install/install-admin-cli-for-vm.sh").read_text()
    m = re.search(r'"\$CLI" "\$DESTDIR(/[^"]+)"', installer)
    assert m, "installer no longer installs $CLI at a fixed path"
    return interp + [m.group(1), *args]


@pytest.fixture
def real(tmp_path, monkeypatch):
    pytest.importorskip("dbus")
    import dbus
    import qdistro_admin_broker as B
    from test_broker_check_permission import NON_ADMIN_UID, _StubBroker

    rules = tmp_path / "rules"
    rules.mkdir()
    broker = _StubBroker(str(tmp_path / "approvals.sqlite"),
                         str(tmp_path / "audit.sqlite"), str(rules))
    monkeypatch.setattr(B, "_read_proc_selinux_label", lambda _pid: "")

    class _Iface:
        def GetPending(self):
            return broker.GetPending()

        before_decide = []   # hooks run just before the CLI's decision

        def DecideRequest(self, rid, decision, scope):
            for hook in self.before_decide:
                hook(rid)
            return broker.DecideRequest(rid, decision, scope)

        def ListHistory(self, limit):
            return broker.ListHistory(limit)

    iface = _Iface()
    monkeypatch.setattr(cli, "_broker", lambda: (iface, dbus))

    def as_peer(argv, uid=0, exe="/usr/bin/python3.13"):
        # /proc/<pid>/exe of a Python script is the interpreter, never the
        # script; the broker must bind the CLI through argv.
        broker.set_peer(uid=uid, pid=4321, exe=exe)
        monkeypatch.setattr(B, "_read_proc_cmdline", lambda _pid: list(argv))

    def enqueue(action="qdistro.test.cli-real", **kw):
        return broker._enqueue(NON_ADMIN_UID, 0, "/usr/bin/requester", 0,
                               action, {}, delegated=kw.get("delegated", False),
                               one_shot=kw.get("one_shot", False))

    def waiter(rid):
        got = []
        broker._pending[rid].waiters.append((got.append, lambda e: got.append(e)))
        return got

    broker.cli_iface = iface
    return broker, as_peer, enqueue, waiter, NON_ADMIN_UID, tmp_path


def _audit_rows(tmp_path):
    return sqlite3.connect(str(tmp_path / "audit.sqlite")).execute(
        "SELECT caller_uid, action, decision, scope, source, approver_uid "
        "FROM audit ORDER BY id").fetchall()


@pytest.mark.parametrize("verb,decision,allowed", [
    ("approve", "allow", True), ("deny", "deny", False)])
def test_installed_cli_decides_through_real_broker(real, capsys, verb,
                                                   decision, allowed):
    broker, as_peer, enqueue, waiter, requester_uid, tmp = real
    rid = enqueue()
    got = waiter(rid)
    as_peer(_installed_cli_argv(verb, str(rid)))

    rc, out, _ = _run(["pending"], capsys)
    assert rc == 0 and "qdistro.test.cli-real" in out

    rc, out, err = _run([verb, str(rid)], capsys)
    assert rc == 0, err
    # the requester's WaitForDecision reply, the signal, and the audit row
    assert got == [allowed]
    assert broker.decided_signals[-1] == (rid, decision)
    assert broker.GetPending() == []
    assert _audit_rows(tmp)[-1] == (requester_uid, "qdistro.test.cli-real",
                                    int(allowed), "once", "prompt", 0)


@pytest.mark.parametrize("argv", [
    # source-tree invocation: not the installed, trusted path
    ["/usr/bin/python3", "cli/qdistro_approvals.py", "approve", "1"],
    ["/usr/bin/python3", "/tmp/qdistro-approvals", "approve", "1"],
])
def test_untrusted_cli_path_is_refused_by_real_broker(real, capsys, argv):
    broker, as_peer, enqueue, waiter, _uid, tmp = real
    rid = enqueue()
    got = waiter(rid)
    as_peer(argv)
    rc, _out, err = _run(["approve", str(rid)], capsys)
    assert rc == 1
    assert "AccessDenied" in err
    assert got == [] and broker._pending[rid].decision is None


def test_installed_cli_as_non_root_is_refused_by_real_broker(real, capsys):
    broker, as_peer, enqueue, waiter, _uid, _tmp = real
    rid = enqueue()
    as_peer(_installed_cli_argv("approve", str(rid)), uid=1001)
    rc, _out, err = _run(["approve", str(rid)], capsys)
    assert rc == 1 and "AccessDenied" in err
    assert broker._pending[rid].decision is None


def test_real_broker_scope_refusal_surfaces(real, capsys):
    broker, as_peer, enqueue, waiter, _uid, _tmp = real
    rid = enqueue(one_shot=True)
    as_peer(_installed_cli_argv("approve", str(rid)))
    rc, _out, err = _run(["approve", str(rid), "--scope", "24h"], capsys)
    assert rc == 1
    assert "ScopeNotPermitted" in err
    assert broker._pending[rid].decision is None


@pytest.mark.parametrize("verb,other", [("deny", "allow"), ("approve", "deny")])
def test_concurrent_decision_is_not_reported_as_ours(real, capsys, verb, other):
    """Race: another approver (Qt app/TUI, here uid 1000) decides between
    the CLI's GetPending snapshot and its DecideRequest. The broker ignores
    the CLI's call; the CLI must report the decision it did NOT make, not
    `denied`/`approved` with rc 0."""
    import qdistro_admin_broker as B
    broker, as_peer, enqueue, waiter, _uid, _tmp = real
    rid = enqueue()
    got = waiter(rid)
    argv = _installed_cli_argv(verb, str(rid))
    as_peer(argv)

    def competing(_rid):
        broker.set_peer(uid=B.ADMIN_UID, pid=4322, exe="/usr/bin/python3.13")
        B._read_proc_cmdline = lambda _p: ["/usr/bin/python3",
                                           "/usr/local/bin/qdistro-admin-tui"]
        try:
            broker.DecideRequest(rid, other, "once")
        finally:
            as_peer(argv)
    broker.cli_iface.before_decide.append(competing)

    rc, out, err = _run([verb, str(rid)], capsys)
    assert rc == 1, (out, err)
    assert "NOT decided by this command" in err
    assert f"recorded {other}" in err and "by uid 1000" in err
    assert got == [other == "allow"]
    assert out == ""


def test_root_argv_spoof_is_admitted_documented(real, capsys):
    """Documents the trust model, not a boundary: the broker's root check
    admits ANY root Python process that names the CLI path in argv (the
    script need not run). It identifies the genuine tool for honest
    callers; root is fully trusted (it could also use busctl). If this
    starts failing, the broker got stricter: update doc/admin-approval.md."""
    broker, as_peer, enqueue, waiter, _uid, _tmp = real
    rid = enqueue()
    as_peer(["/usr/bin/python3", "-c", "import dbus  # anything",
             "/usr/local/sbin/qdistro-approvals"])
    rc, out, err = _run(["approve", str(rid)], capsys)
    assert rc == 0, err
    assert broker._pending[rid].decision is True
