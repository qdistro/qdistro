"""Broker-side polkit responder.

The session polkit agent runs as the admin uid, and polkitd accepts
AuthenticationAgentResponse2 from uid 0 only — so positive polkit
decisions are delivered by the privileged broker:

- RequestPolkitAuth files a request carrying the polkit cookie and the
  identity list polkit offered; an allow — admin click, rule, cache, or
  hook — makes the broker answer polkitd itself.
- RespondPolkitAuth relays an agent-local pam/fprint verdict.
- CancelPolkitAuth retires the queued request when polkitd cancels the
  auth session, so a dead prompt does not linger in the admin queue.

Stub-broker pattern matches test_broker_concurrency.py: subclass Broker
to bypass dbus.service.Object registration + GLib timers, fake
_peer_info, and intercept the actual polkitd call
(_respond_polkit_call) to record what would have been sent.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

import qdistro_admin_broker as B  # noqa: E402
from qdistro_admin_broker import Broker  # noqa: E402
from qdistro_admin_cache import ApprovalCache  # noqa: E402
from qdistro_admin_audit import AuditLog  # noqa: E402
from qdistro_admin_ratelimit import RateLimiter  # noqa: E402
from qdistro_admin_rules import RulesEngine  # noqa: E402

ADMIN_UID = B.ADMIN_UID
NON_ADMIN_UID = 2000
PEER_EXE = "/usr/bin/test-app"
AGENT_EXE = "/usr/bin/python3"
AGENT_ARGV = ["/usr/bin/python3", "-I",
              "/usr/libexec/qdistro/qdistro_polkit_agent.py"]
AGENT_CGROUP = (f"user.slice/user-{ADMIN_UID}.slice/"
                f"user@{ADMIN_UID}.service/"
                "app.slice/qdistro-polkit-agent.service")

IDENT_ROOT = ("unix-user", {"uid": dbus.UInt32(0)})
IDENT_ADMIN = ("unix-user", {"uid": dbus.UInt32(ADMIN_UID)})
IDENT_GROUP = ("unix-group", {"gid": dbus.UInt32(27)})


class _StubBroker(Broker):
    """Minimal Broker bypassing real D-Bus registration."""

    def __init__(self, cache_db: str, audit_db: str, rules_dir: str):
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict = {}
        self._cancelled_polkit_cookies: dict = {}
        self._announced_polkit: dict = {}
        self.cache = ApprovalCache(cache_db)
        self.audit = AuditLog(audit_db)
        self.rules = RulesEngine(rules_dir)
        self.ratelimit = RateLimiter(limit=10_000, window_s=1.0)
        self._audit_retention_days = 0
        self._io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="stub-broker-io")
        # The default peer is the session polkit agent itself: the three
        # polkit relay methods are bound to its installed script, not
        # merely to ADMIN_UID.
        self._peer_uid = ADMIN_UID
        self._peer_pid = 1
        self._peer_exe = AGENT_EXE
        self._peer_argv = list(AGENT_ARGV)
        self._peer_start = 0
        self._peer_label = ("system_u:system_r:unconfined_t:s0",
                            "unconfined_t")
        self._peer_cgroup_val = AGENT_CGROUP
        self._peer_env_names: set = set()
        self.pending_signals: list[int] = []
        self.decided_signals: list[tuple[int, str]] = []
        # Captured (uid, cookie, identity) tuples from _respond_polkit.
        self.polkit_responded: list[tuple] = []
        self.respond_raises: Exception | None = None
        from qdistro_hook_client import HookClient
        self.hooks = HookClient(enabled=False)

    def set_peer(self, uid: int, pid: int = 100, exe: str = AGENT_EXE,
                 start: int = 0, argv: list | None = None) -> None:
        self._peer_uid = uid
        self._peer_pid = pid
        self._peer_exe = exe
        self._peer_argv = list(AGENT_ARGV) if argv is None else argv
        self._peer_start = start

    def _peer_info(self, sender, conn):
        return (self._peer_uid, self._peer_pid, self._peer_exe,
                self._peer_start)

    def _peer_cmdline(self, pid):
        return self._peer_argv

    def _peer_label_type(self, pid):
        return self._peer_label

    def _peer_cgroup(self, pid):
        return self._peer_cgroup_val

    def _peer_environ_names(self, pid):
        return set(self._peer_env_names)

    def _peer_matches_admin_control(self, *, uid: int, pid: int,
                                    exe: str, method: str = ""
                                    ) -> tuple[bool, str]:
        if int(uid) in (0, ADMIN_UID):
            return True, "test-injected admin-control peer"
        return False, f"uid {uid} is not admin uid {ADMIN_UID}"

    def RequestPending(self, rid):  # type: ignore[override]
        self.pending_signals.append(int(rid))

    def RequestDecided(self, rid, decision):  # type: ignore[override]
        self.decided_signals.append((int(rid), str(decision)))

    def ApprovalRevoked(self, caller_uid, action, exe):  # type: ignore[override]
        pass

    def _respond_polkit_call(self, uid, cookie, identity):  # type: ignore[override]
        if self.respond_raises is not None:
            raise self.respond_raises
        self.polkit_responded.append((uid, cookie, identity))


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


def _file_polkit(broker: _StubBroker, cookie: str = "c1",
                 identities=(), action: str = "qdistro.test.action",
                 details: dict | None = None) -> int:
    return broker.RequestPolkitAuth(
        action, details or {}, cookie, identities)


class TestRequestPolkitAuth:

    def test_files_a_pending_request_carrying_the_polkit_context(
            self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        req = broker._pending[rid]
        assert req.polkit_cookie == "cookie-abc"
        assert req.polkit_identities == [
            ("unix-user", {"uid": dbus.UInt32(0)})]
        assert rid in broker.pending_signals

    def test_restricted_to_the_admin_uid(self, broker):
        broker.set_peer(NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)

    def test_admin_approval_answers_polkitd(self, broker):
        """DecideRequest allow → AuthenticationAgentResponse2 with the
        request's own cookie and an identity from the offered list."""
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        assert broker.DecideRequest(rid, "allow", "once") == "applied"
        assert len(broker.polkit_responded) == 1
        uid, cookie, identity = broker.polkit_responded[0]
        assert uid == ADMIN_UID
        assert cookie == "cookie-abc"
        assert identity == ("unix-user", {"uid": dbus.UInt32(0)})

    def test_deny_answers_nothing(self, broker):
        rid = _file_polkit(broker)
        broker.DecideRequest(rid, "deny", "once")
        assert broker.polkit_responded == []

    def test_an_ordinary_request_never_answers_polkitd(self, broker):
        """RequestPermission carries no cookie — the respond path must
        be a no-op for it."""
        rid = broker.RequestPermission("qdistro.test.action", {})
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded == []

    def test_the_response_identity_comes_from_the_offered_list(
            self, broker):
        """polkitd rejects a response naming an identity it did not
        offer. On this image the list is [unix-user uid=0]; where the
        requesting uid is offered, prefer it."""
        rid = _file_polkit(
            broker, "c1", [IDENT_ROOT])
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded[0][2][1]["uid"] == 0

        rid = _file_polkit(
            broker, "c2", [IDENT_ROOT, IDENT_ADMIN])
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded[1][2][1]["uid"] == ADMIN_UID

    def test_an_empty_identity_list_means_no_response(self, broker,
                                                      capsys):
        rid = _file_polkit(broker, "c3", [])
        broker.DecideRequest(rid, "allow", "once")
        assert broker.polkit_responded == []
        assert "no usable identity" in capsys.readouterr().out

    def test_the_response_precedes_the_waiter_release(self, broker):
        """Ordering matters: the agent completes BeginAuthentication the
        instant its WaitForDecision returns, and polkitd tears the
        cookie's session down then. A response sent after the waiter
        release lands on "No session for cookie" — observed live."""
        rid = _file_polkit(broker, "cookie-x", [IDENT_ROOT])
        seen = []
        def reply(v):
            seen.append(bool(v) and bool(broker.polkit_responded))
        broker._pending[rid].waiters.append((reply, lambda e: None))
        broker.DecideRequest(rid, "allow", "once")
        assert seen == [True], (
            "the waiter fired before AuthenticationAgentResponse2 was "
            "sent — polkitd will have already torn the session down")

    def test_a_respond_failure_is_logged_not_fatal(self, broker, capsys):
        """A stale cookie or dead polkitd must not corrupt the decision —
        it was already made and audited."""
        broker.respond_raises = dbus.DBusException(
            "gone", name="org.freedesktop.DBus.Error.ServiceUnknown")
        rid = _file_polkit(broker, "c4", [IDENT_ROOT])
        assert broker.DecideRequest(rid, "allow", "once") == "applied"
        assert "AuthenticationAgentResponse2 failed" in \
            capsys.readouterr().out

    def test_a_cached_allow_answers_polkitd(self, broker):
        """A 'forever' scope grant auto-approves the next identical
        polkit auth — the response must still be delivered."""
        broker.set_peer(0)
        assert broker.cache.store(ADMIN_UID, "qdistro.test.action",
                                  PEER_EXE, "forever", True, ADMIN_UID)
        broker.set_peer(ADMIN_UID)
        rid = _file_polkit(broker, "cookie-cached", [IDENT_ROOT],
                           details={})
        req = broker._pending[rid]
        assert req.decision is True
        assert len(broker.polkit_responded) == 1
        assert broker.polkit_responded[0][1] == "cookie-cached"


class TestRespondPolkitAuth:

    def test_relays_the_verdict_to_polkitd(self, broker):
        broker.AnnouncePolkitAuth("cookie-9")
        broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])
        assert broker.polkit_responded == [
            (ADMIN_UID, "cookie-9", ("unix-user", {"uid": dbus.UInt32(0)}))]

    def test_restricted_to_the_admin_uid(self, broker):
        broker.set_peer(NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])

    def test_empty_identities_fail_closed(self, broker):
        broker.AnnouncePolkitAuth("cookie-9")
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [])
        assert broker.polkit_responded == []

    def test_an_unannounced_cookie_cannot_be_responded(self, broker):
        """A cookie is a bearer secret: a peer that never announced or
        filed it cannot drive the relay (sol r164 — the secret may have
        been learned, but the unique bus name can't be forged)."""
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("stray-cookie", [IDENT_ROOT])
        assert broker.polkit_responded == []

    def test_respond_is_bound_to_the_announcing_sender(self, broker):
        """The same unique bus name must announce and respond — a
        second connection knowing the cookie is refused."""
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT],
                                     sender=":1.10")
        assert broker.polkit_responded == []
        broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT],
                                 sender=":1.9")
        assert len(broker.polkit_responded) == 1

    def test_respond_releases_a_pending_filed_request(self, broker):
        """A local verdict on a filed cookie resolves the queued
        request too — the prompt is moot and waiters release."""
        rid = _file_polkit(broker, "cookie-x", [IDENT_ROOT])
        replies = []
        broker._pending[rid].waiters.append(
            (replies.append, lambda e: None))
        broker.RespondPolkitAuth("cookie-x", [IDENT_ROOT])
        assert broker._pending[rid].decision is True
        assert replies == [True]
        assert (rid, "allow") in broker.decided_signals
        assert len(broker.polkit_responded) == 1

    def test_respond_for_a_denied_request_is_refused(self, broker):
        rid = _file_polkit(broker, "cookie-y", [IDENT_ROOT])
        broker.DecideRequest(rid, "deny", "once")
        broker.RespondPolkitAuth("cookie-y", [IDENT_ROOT])
        assert broker.polkit_responded == []


class TestAnnouncePolkitAuth:

    def test_a_second_sender_cannot_steal_the_binding(self, broker):
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")
        with pytest.raises(dbus.DBusException):
            broker.AnnouncePolkitAuth("cookie-9", sender=":1.10")
        # The original binding still stands.
        broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT],
                                 sender=":1.9")
        assert len(broker.polkit_responded) == 1

    def test_reannounce_by_the_same_sender_is_a_no_op(self, broker):
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")

    def test_announcing_an_own_cancelled_cookie_keeps_it_dead(
            self, broker, capsys):
        """Cancel-before-announce from the same sender: the cookie must
        stay dead — a respond for it is refused."""
        broker.CancelPolkitAuth("dead", sender=":1.9")
        broker.AnnouncePolkitAuth("dead", sender=":1.9")
        broker.RespondPolkitAuth("dead", [IDENT_ROOT], sender=":1.9")
        assert broker.polkit_responded == []
        assert "already cancelled" in capsys.readouterr().out

    def test_an_empty_cookie_is_a_no_op(self, broker):
        broker.AnnouncePolkitAuth("")
        assert broker._announced_polkit == {}

    def test_announcing_a_cookie_filed_by_another_sender_is_refused(
            self, broker):
        """The first declaration wins: a filed request's cookie cannot
        be rebound by a different connection (sol r165)."""
        rid = broker.RequestPolkitAuth(
            "qdistro.test.action", {}, "cookie-9", [IDENT_ROOT],
            sender=":1.9")
        with pytest.raises(dbus.DBusException):
            broker.AnnouncePolkitAuth("cookie-9", sender=":1.10")
        assert broker._announced_polkit == {}
        # The filing itself is undisturbed.
        assert broker._pending[rid].decision is None
        # ...while the filer's own announce is a harmless no-op.
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")
        assert broker._announced_polkit["cookie-9"][0] == ":1.9"

    def test_the_announce_map_never_evicts_a_live_binding(self, broker):
        """Flood resistance: fresh entries are not evicted to make
        room, so a dummy-cookie flood cannot push out a live binding
        and rebind the cookie to another sender (sol r165)."""
        broker.AnnouncePolkitAuth("real", sender=":1.9")
        with broker._lock:
            now = time.time()
            for i in range(B.POLKIT_CANCELLED_MAX - 1):
                broker._announced_polkit[f"pad-{i}"] = (":1.10", now)
        with pytest.raises(dbus.DBusException):
            broker.AnnouncePolkitAuth("one-more", sender=":1.10")
        # The live binding survived the flood.
        assert broker._announced_polkit["real"][0] == ":1.9"
        broker.RespondPolkitAuth("real", [IDENT_ROOT], sender=":1.9")
        assert len(broker.polkit_responded) == 1

    def test_the_cancel_map_never_evicts_a_live_mark(self, broker):
        broker.CancelPolkitAuth("mine", sender=":1.9")
        with broker._lock:
            now = time.time()
            for i in range(B.POLKIT_CANCELLED_MAX - 1):
                broker._cancelled_polkit_cookies[f"pad-{i}"] = (
                    ":1.10", now)
        with pytest.raises(dbus.DBusException):
            broker.CancelPolkitAuth("one-more", sender=":1.10")
        # Same-sender cancel still holds.
        broker.AnnouncePolkitAuth("mine", sender=":1.9")
        broker.RespondPolkitAuth("mine", [IDENT_ROOT], sender=":1.9")
        assert broker.polkit_responded == []


class TestPolkitCookieOwnershipConflicts:
    """First declaration wins: whichever unique sender announced or
    filed a cookie owns it, and a later conflicting declaration from a
    different sender is refused (sol r165)."""

    def test_filing_a_cookie_announced_by_another_sender_is_refused(
            self, broker):
        """A peer that learned a live cookie cannot file it under its
        own sender to become its owner."""
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")
        with pytest.raises(dbus.DBusException):
            broker.RequestPolkitAuth(
                "qdistro.test.action", {}, "cookie-9", [IDENT_ROOT],
                sender=":1.10")
        assert broker._pending == {}
        # The owner's own filing of an announced cookie still works.
        rid = broker.RequestPolkitAuth(
            "qdistro.test.action", {}, "cookie-9", [IDENT_ROOT],
            sender=":1.9")
        assert rid in broker._pending
        assert broker._pending[rid].filed_by == ":1.9"

    def test_filing_a_cookie_pending_under_another_sender_is_refused(
            self, broker):
        rid = broker.RequestPolkitAuth(
            "qdistro.test.action", {}, "cookie-9", [IDENT_ROOT],
            sender=":1.9")
        with pytest.raises(dbus.DBusException):
            broker.RequestPolkitAuth(
                "qdistro.test.action", {}, "cookie-9", [IDENT_ROOT],
                sender=":1.10")
        assert list(broker._pending) == [rid]

    def test_a_foreign_respond_after_a_conflict_attempt_stays_denied(
            self, broker):
        """The conflict refusal is not itself a binding: the would-be
        thief still cannot respond to the cookie."""
        broker.AnnouncePolkitAuth("cookie-9", sender=":1.9")
        with pytest.raises(dbus.DBusException):
            broker.RequestPolkitAuth(
                "qdistro.test.action", {}, "cookie-9", [IDENT_ROOT],
                sender=":1.10")
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT],
                                     sender=":1.10")
        assert broker.polkit_responded == []


class TestCancelPolkitAuth:

    def test_decides_the_matching_request_deny(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        replies = []
        req = broker._pending[rid]
        req.waiters.append((replies.append, lambda e: None))
        broker.CancelPolkitAuth("cookie-abc")
        req = broker._pending[rid]
        assert req.decision is False
        assert replies == [False]
        assert (rid, "deny") in broker.decided_signals
        assert broker.polkit_responded == []

    def test_an_unknown_cookie_is_a_no_op(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.CancelPolkitAuth("someone-elses-cookie")
        assert broker._pending[rid].decision is None
        assert broker.decided_signals == []

    def test_an_already_decided_request_is_untouched(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.DecideRequest(rid, "allow", "once")
        broker.CancelPolkitAuth("cookie-abc")
        assert broker._pending[rid].decision is True

    def test_a_cancel_that_beats_the_file_predenies_it(self, broker):
        """CancelAuthentication is relayed from the agent's mainloop and
        can arrive before the worker's RequestPolkitAuth — the filing
        must come back decided deny, never a queued prompt for a dead
        auth session (astra r159)."""
        broker.CancelPolkitAuth("ghost")
        rid = _file_polkit(broker, "ghost", [IDENT_ROOT])
        req = broker._pending[rid]
        assert req.decision is False
        assert rid not in broker.pending_signals
        assert broker.polkit_responded == []
        # WaitForDecision answers immediately for a decided request.
        seen = []
        broker.WaitForDecision(rid, seen.append, lambda e: None)
        assert seen == [False]

    def test_a_late_duplicate_file_for_a_cancelled_cookie_denies(
            self, broker):
        rid = _file_polkit(broker, "dup", [IDENT_ROOT])
        broker.CancelPolkitAuth("dup")
        rid2 = _file_polkit(broker, "dup", [IDENT_ROOT])
        assert broker._pending[rid].decision is False
        assert broker._pending[rid2].decision is False
        assert broker.polkit_responded == []

    def test_a_respond_relay_for_a_cancelled_cookie_is_a_no_op(
            self, broker, capsys):
        """PAM/fprint can finish just as polkitd cancels — the relay
        must not answer a dead cookie."""
        broker.AnnouncePolkitAuth("dead")
        broker.CancelPolkitAuth("dead")
        broker.RespondPolkitAuth("dead", [IDENT_ROOT])
        assert broker.polkit_responded == []
        assert "already cancelled" in capsys.readouterr().out

    def test_a_foreign_cancel_does_not_kill_the_filing(self, broker):
        """A cancel from a different unique name must not pre-deny the
        cookie owner's filing — only the owner's own cancel does."""
        broker.CancelPolkitAuth("ghost", sender=":1.10")
        rid = _file_polkit(broker, "ghost", [IDENT_ROOT])
        assert broker._pending[rid].decision is None
        # ...while the owner's own cancel still wins the race.
        broker.CancelPolkitAuth("ghost2", sender=":1.9")
        rid2 = broker.RequestPolkitAuth(
            "qdistro.test.action", {}, "ghost2", [IDENT_ROOT],
            sender=":1.9")
        assert broker._pending[rid2].decision is False

    def test_a_foreign_cancel_leaves_the_pending_request_alive(
            self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.CancelPolkitAuth("cookie-abc", sender=":1.10")
        assert broker._pending[rid].decision is None
        assert (rid, "deny") not in broker.decided_signals

    def test_an_empty_cookie_never_matches_ordinary_requests(
            self, broker):
        """polkit_cookie is "" on non-polkit requests; an empty cancel
        must not sweep the queue."""
        rid = broker.RequestPermission("qdistro.test.action", {})
        broker.CancelPolkitAuth("")
        assert broker._pending[rid].decision is None

    def test_restricted_to_the_admin_uid(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.set_peer(NON_ADMIN_UID)
        with pytest.raises(dbus.DBusException):
            broker.CancelPolkitAuth("cookie-abc")
        broker.set_peer(ADMIN_UID)
        assert broker._pending[rid].decision is None


class TestPolkitAgentPeerBinding:
    """The relay methods must bind the caller to the installed agent
    script — ADMIN_UID alone lets any admin-uid process that learned a
    live cookie file, approve, or cancel a polkit prompt (astra r159
    P1)."""

    def test_an_untrusted_admin_exe_cannot_file(self, broker):
        broker.set_peer(ADMIN_UID, exe=PEER_EXE,
                        argv=[PEER_EXE])
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)
        assert broker._pending == {}

    def test_admin_python_off_the_agent_script_cannot_file(self, broker):
        """`python3 -c ...` as admin must not impersonate the agent."""
        broker.set_peer(ADMIN_UID, exe=AGENT_EXE,
                        argv=["python3", "-c", "evil()"])
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)
        assert broker._pending == {}

    def test_the_script_path_as_an_argument_is_not_the_agent(
            self, broker):
        """`python3 -c 'evil()' /usr/libexec/.../agent.py` puts the
        script at argv[-1] — position alone would admit it, but -c is
        an argument-taking flag outside the safe-flag whitelist
        (sol r161/r165)."""
        broker.set_peer(ADMIN_UID, exe=AGENT_EXE,
                        argv=["python3", "-c", "evil()", AGENT_ARGV[-1]])
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)
        assert broker._pending == {}

    def test_an_argument_taking_flag_before_the_script_is_rejected(
            self, broker):
        """`-W`/`-X`/`-m` consume the next argv element as their
        argument — allowing arbitrary flags before the script would let
        `python3 -W ignore <script>`-style shapes smuggle a non-script
        position or mask `-c`. Only the no-argument isolation flags
        (-I/-E/-s/-P/-u/…) the unit may legitimately use are admitted
        (sol r165)."""
        for argv in (
                ["python3", "-W", "ignore", AGENT_ARGV[-1]],
                ["python3", "-X", "utf8", AGENT_ARGV[-1]],
                ["python3", "-m", "site", AGENT_ARGV[-1]],
                ["python3", "--check-hash-based-pycs", "always",
                 AGENT_ARGV[-1]]):
            broker.set_peer(ADMIN_UID, exe=AGENT_EXE, argv=argv)
            with pytest.raises(dbus.DBusException):
                _file_polkit(broker)
        assert broker._pending == {}

    def test_injection_capable_environment_is_rejected(self, broker):
        """A same-uid caller can push PYTHONPATH/LD_PRELOAD into the
        user manager's environment and restart the unit — startup code
        would then share the agent's trusted connection (sol r165).
        The peer's environ names must carry none of them."""
        for env in ({"PYTHONPATH"}, {"LD_PRELOAD"}, {"PYTHONHOME"},
                    {"PYTHONSTARTUP"}, {"BASH_ENV"}, {"LD_AUDIT"},
                    {"PYTHONBREAKPOINT"},
                    {"QDISTRO_POLKIT_NONINTERACTIVE"}):
            broker._peer_env_names = set(env)
            with pytest.raises(dbus.DBusException):
                _file_polkit(broker)
            broker._peer_env_names = set()
        assert broker._pending == {}

    def test_matching_argv_outside_the_agent_unit_is_rejected(
            self, broker):
        """PYTHONPATH/usercustomize lets an attacker run code inside a
        process whose argv matches the agent — so the cgroup must also
        match the agent unit's exact path (sol r162)."""
        broker._peer_cgroup_val = (
            f"user.slice/user-{ADMIN_UID}.slice/"
            f"user@{ADMIN_UID}.service/"
            "session.slice/session-42.scope")
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)
        assert broker._pending == {}
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])
        assert broker.polkit_responded == []

    def test_a_same_named_child_cgroup_is_not_the_unit(self, broker):
        """A delegated scope can contain a child cgroup literally named
        `qdistro-polkit-agent.service` — suffix matching would pass it
        while the real unit still runs (sol r163). Only the exact
        systemd-anchored path is the unit."""
        base = (f"user.slice/user-{ADMIN_UID}.slice/"
                f"user@{ADMIN_UID}.service")
        for forged in (
                f"{base}/app.slice/run-u7.scope/qdistro-polkit-agent.service",
                f"{base}/evil.slice/qdistro-polkit-agent.service",
                f"{base}/app.slice/nested/qdistro-polkit-agent.service"):
            broker._peer_cgroup_val = forged
            with pytest.raises(dbus.DBusException):
                _file_polkit(broker)
        assert broker._pending == {}

    def test_an_empty_cgroup_fails_closed(self, broker):
        broker._peer_cgroup_val = ""
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)
        assert broker._pending == {}

    def test_a_hostile_selinux_type_rejects_even_root(self, broker):
        """uid 0 in a container/tier domain must not reach the relay."""
        broker.set_peer(0, exe=PEER_EXE, argv=[PEER_EXE])
        broker._peer_label = (
            "system_u:system_r:container_t:s0", "container_t")
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])
        assert broker.polkit_responded == []

    def test_an_untrusted_admin_exe_cannot_respond(self, broker):
        broker.set_peer(ADMIN_UID, exe=PEER_EXE,
                        argv=[PEER_EXE])
        with pytest.raises(dbus.DBusException):
            broker.RespondPolkitAuth("cookie-9", [IDENT_ROOT])
        assert broker.polkit_responded == []

    def test_an_untrusted_admin_exe_cannot_cancel(self, broker):
        rid = _file_polkit(broker, "cookie-abc", [IDENT_ROOT])
        broker.set_peer(ADMIN_UID, exe=PEER_EXE,
                        argv=[PEER_EXE])
        with pytest.raises(dbus.DBusException):
            broker.CancelPolkitAuth("cookie-abc")
        assert broker._pending[rid].decision is None

    def test_a_hostile_selinux_type_is_rejected(self, broker):
        """An admin-uid process in a container/tier domain running the
        agent argv still must not reach the relay."""
        broker._peer_label = (
            "system_u:system_r:qdistro_tier3_t:s0", "qdistro_tier3_t")
        with pytest.raises(dbus.DBusException):
            _file_polkit(broker)
        assert broker._pending == {}

    def test_root_needs_no_relay(self, broker):
        """uid 0 may answer polkitd directly; the broker does not
        pretend to gate it."""
        broker.set_peer(0, exe=PEER_EXE, argv=[PEER_EXE])
        rid = _file_polkit(broker, "cookie-root", [IDENT_ROOT])
        assert rid in broker._pending


class TestStalePolkitReap:
    """Undecided polkit requests outliving the agent's wait bound belong
    to dead auth sessions — they must not sit in the queue awaiting an
    approval that would respond into a void (astra r159)."""

    def test_a_stale_undecided_polkit_request_is_denied_and_released(
            self, broker):
        rid = _file_polkit(broker, "stale", [IDENT_ROOT])
        replies = []
        broker._pending[rid].waiters.append(
            (replies.append, lambda e: None))
        broker._pending[rid].created_at = \
            time.time() - B.POLKIT_UNDECIDED_TTL_S - 1
        broker._reap_pending()
        req = broker._pending[rid]
        assert req.decision is False
        assert replies == [False]
        assert (rid, "deny") in broker.decided_signals
        assert broker.polkit_responded == []

    def test_a_fresh_undecided_polkit_request_survives(self, broker):
        rid = _file_polkit(broker, "fresh", [IDENT_ROOT])
        broker._reap_pending()
        assert broker._pending[rid].decision is None

    def test_an_undecided_ordinary_request_is_never_reaped(self, broker):
        """An admin prompt may legitimately wait on a human
        indefinitely — only polkit-filed requests have a session
        deadline."""
        rid = broker.RequestPermission("qdistro.test.action", {})
        broker._pending[rid].created_at = \
            time.time() - 10 * B.POLKIT_UNDECIDED_TTL_S
        broker._reap_pending()
        assert broker._pending[rid].decision is None


class TestIdentityNormalization:

    def test_numeric_fields_stay_uint32_rest_become_strings(self):
        idents = B._polkit_identities([
            ("unix-user", {"uid": dbus.UInt32(0), "name": "root"}),
            ("unix-group", {"gid": dbus.UInt32(27)}),
        ])
        assert idents == [
            ("unix-user", {"uid": dbus.UInt32(0), "name": "root"}),
            ("unix-group", {"gid": dbus.UInt32(27)}),
        ]

    def test_malformed_entries_are_dropped(self):
        assert B._polkit_identities([None, "junk", IDENT_ADMIN]) == [
            ("unix-user", {"uid": dbus.UInt32(ADMIN_UID)})]

    def test_pick_prefers_the_requesting_uid(self):
        picked = B._pick_polkit_identity(
            [IDENT_ROOT, IDENT_ADMIN], ADMIN_UID)
        assert picked[1]["uid"] == ADMIN_UID

    def test_pick_falls_back_to_first_unix_user_then_first_entry(self):
        # first unix-user wins over an earlier non-unix-user entry
        assert B._pick_polkit_identity(
            [IDENT_GROUP, IDENT_ROOT], ADMIN_UID)[0] == "unix-user"
        assert B._pick_polkit_identity([IDENT_GROUP], ADMIN_UID) == \
            ("unix-group", {"gid": dbus.UInt32(27)})
        assert B._pick_polkit_identity([], ADMIN_UID) is None
