"""Every command the session manager runs to completion is bounded, and handled.

("To completion" is the precise claim: the `ip monitor link` event stream is
deliberately unbounded and lives on its own watcher thread. It is the single
allowlisted exemption, pinned by name and count in TestEveryChildIsBounded.)

Two layers:

* ``TestEveryChildIsBounded`` is a source sweep. It is the regression guard:
  a new ``subprocess.run`` with no ``timeout=`` fails here, in the daemon whose
  D-Bus methods — all of them except StopSilo — run synchronously on the GLib
  main loop, so an unbounded child is a service-wide outage and not a slow call.

* The rest pin what each site DOES when the bound fires. That is the load-bearing
  half: ``timeout=`` alone converts a hang into a ``TimeoutExpired``, which is
  neither a ``CalledProcessError`` nor an ``OSError`` and would sail straight
  through several handlers that were written to contain exactly this failure.
  Each test below names the answer the site owes its caller — fail closed, fail
  safe, or propagate — so a later "simplification" cannot quietly change it.
"""

from __future__ import annotations

import ast
import math
import types

import pytest

sm = pytest.importorskip("qdistro_session_manager")


def _timeout_on(match, *, timeout=1):
    """A subprocess.run stand-in that wedges only for argv containing `match`.

    Selective rather than blanket: several of these methods run more than one
    child (nft element add first calls _nft_ensure_table), and a blanket raise
    would trip the FIRST one and test nothing about the site under examination.
    """
    def fake_run(argv, **kw):
        if match in " ".join(str(a) for a in argv):
            raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout", timeout))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    return fake_run


class TestEveryChildIsBounded:
    """No subprocess.run-family call in the daemon may omit a FINITE timeout."""

    # `ip monitor link` is an event STREAM, not a command that completes: it is
    # supposed to run until stop_link_watcher() kills it. It is the one
    # legitimate exemption, so it is named here — by the call's full string
    # literals rather than a substring — instead of being left to a reviewer's
    # judgement. NB the extractor joins string constants and discards dynamic
    # expressions, so this pins the argv's literal parts, not every future
    # expression that could appear between them.
    ALLOWED_UNBOUNDED = {("Popen", "ip netns exec ip monitor link")}

    def _calls(self):
        src = open(sm.__file__).read()
        tree = ast.parse(src)
        out = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # A call may pass `timeout=<parameter>`; record the enclosing
            # function so the parameter can be resolved to its default.
            for node in ast.walk(func):
                fn = getattr(node, "func", None)
                if not (isinstance(node, ast.Call)
                        and isinstance(fn, ast.Attribute)
                        and isinstance(fn.value, ast.Name)
                        and fn.value.id == "subprocess"
                        and fn.attr in {"run", "call", "check_call",
                                        "check_output", "Popen"}):
                    continue
                argv = " ".join(
                    a.value for a in ast.walk(node.args[0] if node.args else node)
                    if isinstance(a, ast.Constant) and isinstance(a.value, str))
                kw = {k.arg: k.value for k in node.keywords}
                out.append((node.lineno, fn.attr, argv, kw, func))
        return out

    @staticmethod
    def _param_default(func, name):
        """The default of parameter `name` of `func`, as an AST node.

        A call may legitimately pass `timeout=<parameter>` (systemctl_stop
        does, so its callers can choose a shorter bound). Resolving the default
        keeps the guard strict rather than widening it: a parameter with NO
        default, or one defaulting to None/inf, still fails.
        """
        a = func.args
        for args, defaults in ((a.args + a.posonlyargs, a.defaults),
                               (a.kwonlyargs, a.kw_defaults)):
            named = [x.arg for x in args]
            if name not in named:
                continue
            # Positional defaults are right-aligned; kwonly defaults align 1:1.
            if defaults is a.kw_defaults:
                return defaults[named.index(name)]
            off = len(named) - len(defaults)
            i = named.index(name) - off
            return defaults[i] if i >= 0 else None
        return None

    @staticmethod
    def _bound_seconds(node, func=None):
        """The timeout's VALUE in seconds, or None if it is not a finite number.

        Resolving this matters more than it looks: `timeout=None` is what
        subprocess treats as "wait forever", and it satisfies any check that
        only asks whether the keyword was written. A constant reference is
        resolved through the module so the named bounds count.
        """
        if node is None:
            return None
        if isinstance(node, ast.Constant):
            val = node.value
        elif isinstance(node, ast.Name):
            val = getattr(sm, node.id, None)
            if val is None and func is not None:
                d = TestEveryChildIsBounded._param_default(func, node.id)
                return TestEveryChildIsBounded._bound_seconds(d)
        else:
            return None
        # math.isfinite is the load-bearing half. `timeout=1e309` evaluates to
        # positive infinity: it is an int/float, it is > 0, and it removes the
        # deadline just as completely as `timeout=None`. A guard that only
        # asks "is it a positive number?" is green for an unbounded child.
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return None
        return val if math.isfinite(val) else None

    def test_at_least_one_call_was_found(self):
        # Guards the sweep itself: an AST change that stopped matching would
        # otherwise make this whole class pass vacuously forever.
        assert len(self._calls()) > 20

    def test_no_unbounded_child(self):
        bad = []
        for lineno, attr, argv, kw, func in self._calls():
            if any(attr == a and argv == m for a, m in self.ALLOWED_UNBOUNDED):
                continue
            secs = self._bound_seconds(kw.get("timeout"), func)
            if secs is None:
                why = ("no timeout=" if "timeout" not in kw
                       else "timeout= is not a FINITE number — None and inf "
                            "both wait forever")
            elif secs <= 0:
                why = f"timeout={secs} is not a positive bound"
            else:
                continue
            bad.append(f"{sm.__file__}:{lineno} subprocess.{attr}({argv!r}): {why}")
        assert not bad, (
            "unbounded child process(es) in the session manager — every D-Bus "
            "method but StopSilo runs on the main loop, so these are outages:\n"
            + "\n".join(bad))

    def test_the_exemption_matches_exactly_one_call(self):
        # If the link watcher gains a bound (or goes away), the allowlist entry
        # is dead weight that would silently excuse a future unbounded call;
        # if a SECOND call ever matched it, the exemption would have widened
        # without anyone deciding to widen it.
        for attr, argv in self.ALLOWED_UNBOUNDED:
            n = sum(1 for _l, a, v, _k, _f in self._calls()
                    if a == attr and v == argv)
            assert n == 1, f"exemption {attr}({argv!r}) matched {n} call(s)"


class TestBoundValues:
    ALL = ("_T_NETLINK", "_T_ACCOUNT", "_T_BTRFS", "_T_SYSTEMCTL",
           "_T_SYSTEMCTL_STOP", "_T_SYSTEMCTL_CANCEL", "_T_PODMAN",
           "_T_DNSMASQ")

    def test_every_bound_is_finite_and_positive(self):
        for name in self.ALL:
            v = getattr(sm, name)
            assert isinstance(v, (int, float)) and not isinstance(v, bool), name
            assert v > 0, name
            # Infinity is a positive number and no bound at all.
            assert math.isfinite(v), name

    def test_stop_bound_covers_more_than_one_timeoutstopsec_phase(self):
        # `systemctl stop` is not one 90s allowance. TimeoutStopSec applies
        # separately to the ExecStop command and then to termination of the
        # service processes, and the tier-2 helper does rootless-podman setup
        # before its own `stop -t 10`. A bound that only cleared a single 90s
        # phase could expire on a stop still progressing legally and report a
        # failure for a teardown about to succeed.
        #
        # This asserts the SHAPE of the reasoning, not a proven contract: no
        # timing study backs the number, and a deployed manager could override
        # DefaultTimeoutStopSec. It is a wedge catcher with room for the phases.
        assert sm._T_SYSTEMCTL_STOP >= 2 * 90

    def test_the_quick_bounds_stay_quick(self):
        # The per-operation bounds that have no excuse to be long. These are the
        # ones on the main loop that a wedge would turn into a visible outage.
        for name in ("_T_NETLINK", "_T_DNSMASQ"):
            assert getattr(sm, name) <= 30, name
        for name in ("_T_SYSTEMCTL", "_T_PODMAN", "_T_BTRFS",
                     "_T_SYSTEMCTL_CANCEL"):
            assert getattr(sm, name) <= 60, name

    def test_the_compensating_stop_is_not_the_teardown_bound(self):
        # The compensating stop after a timed-out start runs on the MAIN LOOP
        # (StartSilo and LaunchPodApp are synchronous), stacked on top of the
        # start bound. Using the 300s teardown allowance there would hand the
        # loop a 5-minute outage, so it gets its own short bound — safe only
        # because not confirming the cancellation is not treated as success.
        assert sm._T_SYSTEMCTL_CANCEL < sm._T_SYSTEMCTL_STOP
        assert sm._T_SYSTEMCTL + sm._T_SYSTEMCTL_CANCEL <= 60

    def test_account_bound_is_a_deliberate_main_loop_outlier(self):
        """_T_ACCOUNT is long ON PURPOSE, and that is a trade, not an oversight.

        useradd -m / userdel -r are dominated by the home TREE, and they are
        destructive: killing `userdel -r` partway leaves a half-removed home
        that no rollback repairs, while delete() rolls the silo back to Stopped.
        Between "the daemon is unresponsive for a few minutes" (recoverable) and
        "the user's home is half-deleted" (not), blocking is the lesser harm —
        so this bound is sized to sit well clear of a large home rather than to
        protect the loop. It is NOT a size-independent guarantee: a big enough
        tree still exceeds it, which is the other half of why the real answer is
        to move this work off the loop. Getting BOTH requires moving the work off the main
        loop, which is filed in todo/open-followups.md and is not this change.

        Pinned so the trade cannot be silently reversed by someone tightening
        the number to make the loop look better.
        """
        assert sm._T_ACCOUNT >= 300


class TestTimedOutRunIsNotBenign:
    def test_timeout_message_is_not_mistaken_for_a_benign_nft_error(self):
        # The nft element sites treat "file exists"/"no such file" as success.
        # If a timeout's stand-in stderr ever matched one of those, a timed-out
        # ADD would be read as "the backstop is already installed" — a silo
        # running with no egress kill-switch and no error anywhere.
        r = sm._TimedOutRun("nft add element timed out after 15s")
        assert r.returncode != 0
        assert not sm._nft_benign(r.stderr)


class TestFailClosedReaders:
    """Timeout must produce the SAFE answer, never the convenient one."""

    def test_tier2_silo_running_is_active_timeout_reports_running(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("is-active"))
        assert sm._SystemOps().tier2_silo_running("work") is True

    def test_tier2_silo_running_podman_timeout_reports_running(self, monkeypatch):
        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "is-active" in argv:
                return types.SimpleNamespace(
                    returncode=0, stdout="inactive\n", stderr="")
            raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        # A wedged rootless podman is the exact case: "the check did not run"
        # must never be reported as a clean stop.
        assert sm._SystemOps().tier2_silo_running("work") is True

    def test_nft_table_present_timeout_assumes_present(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("list table"))
        # "Present" means the removal path still attempts the element delete
        # (itself fail-safe) rather than skipping it on an unanswered check.
        assert sm._SystemOps()._nft_table_present() is True

    def test_disp_container_list_timeout_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("podman ps"))
        assert sm._SystemOps().disp_container_list() == []


class TestFatalSitesPropagate:
    """Apply-path failures must reach the caller's fail-closed handler."""

    def test_useradd_timeout_propagates(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("useradd"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().useradd("work", 2000)

    def test_userdel_timeout_becomes_calledprocesserror(self, monkeypatch):
        # delete() and its rollback are written against CalledProcessError from
        # this method; a bare TimeoutExpired would still be caught by the broad
        # handler, but the typed failure keeps the contract honest and keeps the
        # "silo is undeletable" regression at line 724 from coming back.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("userdel"))
        with pytest.raises(sm.subprocess.CalledProcessError) as ei:
            sm._SystemOps().userdel("work")
        assert "timed out" in (ei.value.stderr or "")

    def test_daemon_reload_timeout_propagates(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("daemon-reload"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().daemon_reload()

    def test_nft_ensure_table_timeout_raises_runtimeerror(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("nft -f"))
        with pytest.raises(RuntimeError, match="timed out"):
            sm._SystemOps()._nft_ensure_table()

    def test_nft_skuid_drop_enable_timeout_raises(self, monkeypatch):
        # Fatal on ADD: a silo whose uid backstop was not installed must not
        # come up. The element call wedges; _nft_ensure_table succeeds.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("blocked_uids"))
        with pytest.raises(RuntimeError, match="timed out"):
            sm._SystemOps().nft_skuid_drop(2000, True)

    def test_nat_masquerade_enable_timeout_raises(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("nat_subnets"))
        with pytest.raises(RuntimeError, match="timed out"):
            sm._SystemOps().nat_masquerade("10.77.0.0/24", True)

    def test_ip_apply_timeout_propagates(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("addr"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().addr_add("ns0", "veth0", "10.77.0.2/24")

    def test_enable_ip_forward_timeout_propagates(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("ip_forward"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().enable_ip_forward()


class TestCheckFalseStillPropagatesAWedge:
    """`check=False` means "ignore a non-zero exit status", NOT "ignore a wedge".

    These three are teardown-flavoured, so swallowing a timeout looks tempting.
    It is a fail-OPEN. link_del() and the netns/ipv6 ops all run on the APPLY
    path (EgressBackend.apply tears stale devices down first), where a
    swallowed timeout lets apply(none) return dark=True while the old wg device
    is still up with its default route — a silo reported as networkless that
    still has a working tunnel. "The delete failed" and "the device was already
    absent" must never collapse into the same answer.
    """

    def test_link_del_timeout_propagates(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("link del"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().link_del("ns0", "wg-2000")

    def test_netns_remove_timeout_propagates(self, monkeypatch):
        # The two callers already wrap this in "log and continue"; that policy
        # belongs to them, not to a method that would hide the distinction.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("netns del"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().netns_remove("ns0")

    def test_ipv6_disable_timeout_propagates(self, monkeypatch):
        # Runs on the `direct` apply path immediately before the silo is
        # declared non-dark: swallowing it hands out a silo whose v6 SLAAC path
        # around the NAT was never actually closed.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("disable_ipv6"))
        with pytest.raises(sm.subprocess.TimeoutExpired):
            sm._SystemOps().ipv6_disable("ns0", "veth0")

    def test_a_nonzero_exit_is_still_ignored(self, monkeypatch):
        # The other half of the contract, so the fix above cannot be "read" as
        # making check=False strict: a device that is simply already gone must
        # still be a silent no-op.
        #
        # The fake HONOURS check=, which is the whole point — a fake that
        # ignores it passes this test no matter what the caller requested, and
        # a `check=check` quietly changed to `check=True` would sail through.
        def rc_1(argv, **kw):
            if kw.get("check"):
                raise sm.subprocess.CalledProcessError(
                    1, [str(a) for a in argv])
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", rc_1)
        sm._SystemOps().link_del("ns0", "wg-2000")        # must not raise
        sm._SystemOps().netns_remove("ns0")

    def test_the_apply_path_still_checks_its_exit_status(self, monkeypatch):
        # And the converse: check=True callers must still fail on a non-zero
        # exit, so "stop swallowing timeouts" cannot be implemented by making
        # every _ip() call check=False.
        def rc_1(argv, **kw):
            if kw.get("check"):
                raise sm.subprocess.CalledProcessError(
                    1, [str(a) for a in argv])
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", rc_1)
        with pytest.raises(sm.subprocess.CalledProcessError):
            sm._SystemOps().addr_add("ns0", "veth0", "10.77.0.2/24")


class TestTimedOutStartIsAlwaysUnresolved:
    """A start timeout can never be established as cancelled, so it never is.

    Three separate reasons, each enough on its own:
      * issuing a compensating stop is not evidence PID 1 accepted it (an
        irreversible conflicting job can block the replacement transaction);
      * `is-active` reporting inactive is not evidence either — a job WAITING
        on a dependency has not begun executing, so the unit reads inactive
        while the start is still queued and can launch later;
      * for a tier-2 silo an inactive unit says nothing about the workload: the
        rootless container lives outside the unit cgroup and its ExecStop is
        best-effort, so the supervisor can exit with the container alive.

    Earlier rounds of this change tried to establish cancellation cheaply and
    were wrong each time. The method now does not try: it hands the question to
    StopSilo, which already answers it properly, and pins the silo ACTIVE
    meanwhile.
    """

    UNIT = "qdistro-tier2-silo@work.service"

    def _ops(self, monkeypatch, stop_ok=True):
        calls = []

        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            calls.append(argv)
            if "start" in argv:
                raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if "stop" in argv and not stop_ok:
                raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        return sm._SystemOps(), calls

    def test_a_timeout_raises_start_not_cancelled(self, monkeypatch):
        ops, _ = self._ops(monkeypatch)
        with pytest.raises(sm.StartNotCancelled):
            ops.systemctl_start(self.UNIT)

    def test_a_best_effort_stop_is_still_issued(self, monkeypatch):
        # Not consulted, but still worth attempting: it is the only chance to
        # actually cancel a job that has not begun.
        ops, calls = self._ops(monkeypatch)
        with pytest.raises(sm.StartNotCancelled):
            ops.systemctl_start(self.UNIT)
        assert ["systemctl", "stop", self.UNIT] in calls, calls

    def test_no_liveness_query_is_used_as_evidence(self, monkeypatch):
        # An `is-active` here would be a third main-loop bound AND a false
        # signal: inactive does not mean the queued job will not launch.
        ops, calls = self._ops(monkeypatch)
        with pytest.raises(sm.StartNotCancelled):
            ops.systemctl_start(self.UNIT)
        assert not any("is-active" in c for c in calls), calls

    def test_a_successful_stop_is_not_treated_as_confirmation(self, monkeypatch):
        ops, _ = self._ops(monkeypatch, stop_ok=True)
        with pytest.raises(sm.StartNotCancelled):
            ops.systemctl_start(self.UNIT)

    def test_a_stop_that_never_completes_is_not_confirmation(self, monkeypatch):
        ops, _ = self._ops(monkeypatch, stop_ok=False)
        with pytest.raises(sm.StartNotCancelled):
            ops.systemctl_start(self.UNIT)

    def test_the_compensating_stop_uses_the_short_main_loop_bound(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "start" in argv:
                raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
            if "stop" in argv:
                seen["stop"] = kw.get("timeout")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        with pytest.raises(sm.StartNotCancelled):
            sm._SystemOps().systemctl_start(self.UNIT)
        # Not the 300s teardown allowance: this runs on the main loop.
        assert seen["stop"] == sm._T_SYSTEMCTL_CANCEL

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path

    def test_the_error_names_the_remedy(self, monkeypatch):
        """The operator guidance is load-bearing, not decoration.

        `start()` from ACTIVE is an idempotent no-op that reports success, so
        "just retry StartSilo" would silently launch nothing and look fine. The
        recovery is StopSilo (which runs the real verified teardown, including
        the tier-2 container check) and only then StartSilo. If that sentence
        is ever dropped, the failure becomes very hard to diagnose.
        """
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=self.tmp / "silos.yaml")
        store.create("work", 2000)
        ops.systemctl_start = lambda unit: (_ for _ in ()).throw(
            sm.StartNotCancelled("unresolved"))
        with pytest.raises(sm.StartNotCancelled) as ei:
            store.start("work")
        msg = str(ei.value)
        assert "Stop it" in msg and "no-op" in msg, msg

    def test_a_clean_start_issues_nothing_else(self, monkeypatch):
        calls = []

        def fake_run(argv, **kw):
            calls.append([str(a) for a in argv])
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        sm._SystemOps().systemctl_start(self.UNIT)
        assert calls == [["systemctl", "start", self.UNIT]], calls


class TestUncertainStartLeavesTheSiloActive:
    """The lifecycle answer to StartNotCancelled: ACTIVE, never STOPPED.

    Recording STOPPED for a start we could not undo makes the next StopSilo a
    no-op (it returns early on State.STOPPED) and admits DeleteSilo over a
    possibly live workload. ACTIVE is honest, retryable, and not deletable.
    """

    def test_start_not_cancelled_is_a_session_error(self):
        # So the D-Bus layer maps it to a domain-typed error rather than a
        # generic Python exception the client cannot classify.
        assert issubclass(sm.StartNotCancelled, sm.SessionError)
        assert sm.StartNotCancelled.dbus_name == "StartNotCancelled"

    def test_active_is_not_deletable_but_stopped_is(self):
        """The state machine is WHY ACTIVE is the right answer, so pin it here.

        Without this the rollback test only asserts that the code says ACTIVE,
        not that ACTIVE buys anything. If DELETING ever became reachable from
        ACTIVE, the uncertain-start fix would silently stop protecting anything
        while every other test stayed green.
        """
        from qdistro_session_manager import State, _STATE_TRANSITIONS
        assert State.DELETING not in _STATE_TRANSITIONS[State.ACTIVE]
        assert State.DELETING in _STATE_TRANSITIONS[State.STOPPED]
        # ...and the silo must still be stoppable, or "retryable" is a lie.
        assert State.STOPPING in _STATE_TRANSITIONS[State.ACTIVE]

    def test_an_uncertain_start_really_leaves_the_silo_active(self, tmp_path):
        """BEHAVIOURAL, not a substring check.

        A source-shape assertion cannot see control flow: dropping the `raise`
        after `_force_state(..., ACTIVE)` leaves every substring in place while
        execution falls through to the egress teardown and `State.STOPPED`
        below it. Codex found exactly that mutation surviving 57 tests. Drive
        the real store instead and assert the state it lands in.
        """
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)

        def uncertain(unit):
            raise sm.StartNotCancelled(f"start of {unit} unresolved")
        ops.systemctl_start = uncertain

        with pytest.raises(sm.StartNotCancelled):
            store.start("work")
        assert store.get("work").state == sm.State.ACTIVE, \
            "an unresolved start was recorded as STOPPED"

    def test_an_uncertain_start_is_not_deletable(self, tmp_path):
        # The whole point of ACTIVE: DeleteSilo must be refused while the
        # workload may still be live.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        ops.systemctl_start = lambda unit: (_ for _ in ()).throw(
            sm.StartNotCancelled("unresolved"))
        with pytest.raises(sm.StartNotCancelled):
            store.start("work")
        with pytest.raises(sm.SessionError):
            store.delete("work")
        assert store.get("work").state == sm.State.ACTIVE

    def test_an_uncertain_start_is_still_stoppable(self, tmp_path):
        # "Retryable" has to mean something: StopSilo from ACTIVE runs the real
        # teardown, which is the path that CAN establish the truth.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        ops.systemctl_start = lambda unit: (_ for _ in ()).throw(
            sm.StartNotCancelled("unresolved"))
        with pytest.raises(sm.StartNotCancelled):
            store.start("work")
        store.stop("work", 0)
        assert store.get("work").state == sm.State.STOPPED

    def test_an_uncertain_start_keeps_the_egress_up(self, tmp_path):
        # Tearing the netns down under a workload that may still be running
        # would strand it on a half-removed network. The teardown belongs to
        # the stop path, which happens after we know.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000, egress="none")
        ops.systemctl_start = lambda unit: (_ for _ in ()).throw(
            sm.StartNotCancelled("unresolved"))
        with pytest.raises(sm.StartNotCancelled):
            store.start("work")
        assert not any(c[0] == "netns_remove" for c in ops.egress_calls), \
            ops.egress_calls

    def test_an_ordinary_start_failure_still_rolls_back_to_stopped(self, tmp_path):
        # The converse, so "leave it ACTIVE" cannot be over-applied: a start
        # that definitively failed must still be recorded STOPPED, or every
        # failed launch would become an undeletable silo.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        ops.systemctl_start = lambda unit: (_ for _ in ()).throw(
            sm.subprocess.CalledProcessError(1, ["systemctl", "start", unit]))
        with pytest.raises(sm.SessionError):
            store.start("work")
        assert store.get("work").state == sm.State.STOPPED


class TestBestEffortSitesDoNotRaise:
    """The sites that genuinely promise never to raise, and why each may.

    A timeout is a failure like any other here — it must not become the one
    failure that wedges a delete or a stop — but this list is short on purpose.
    Every entry has a caller that establishes the truth for itself afterwards,
    or a written contract that the operation is advisory.
    """

    def test_systemctl_stop_timeout_is_silent(self, monkeypatch):
        # The caller decides the outcome via tier2_silo_running(); this method
        # raising would skip that fail-closed check entirely.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("stop"))
        sm._SystemOps().systemctl_stop("qdistro-tier2-silo@work.service")

    def test_reload_dbus_timeout_is_silent(self, monkeypatch):
        # The grant is already on disk; the bus rereads it at next start.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("reload"))
        sm._SystemOps()._reload_dbus()

    def test_nft_skuid_drop_disable_timeout_is_silent(self, monkeypatch):
        # Fail-SAFE direction: a backstop we could not remove still blocks.
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("blocked_uids"))
        sm._SystemOps().nft_skuid_drop(2000, False)

    def test_nat_masquerade_disable_timeout_is_silent(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("nat_subnets"))
        sm._SystemOps().nat_masquerade("10.77.0.0/24", False)


class TestBtrfsFallsThrough:
    """A wedged btrfs must not fail the create for the CONVERSION's sake.

    TimeoutExpired is neither CalledProcessError nor OSError, so before it was
    named in the except clause a btrfs wedge escaped
    _convert_home_to_subvolume, escaped useradd(), and failed the whole
    CreateSilo — even though btrfs trouble is supposed to fall through.

    "Fall through" is conditional, and deliberately so: the recovery RAISES if
    it cannot leave a home that exists and is hardened, because an absent or
    world-readable home is worse than a failed create. Nor is the home
    necessarily a plain directory afterwards — if the subvolume was created
    before the failure it stays a subvolume; only the conversion was abandoned.
    """

    def _reroot(self, monkeypatch, tmp_path):
        import pathlib as _pl
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(_pl.Path(*a)).lstrip("/"))

    def test_probe_timeout_leaves_the_home_untouched(self, monkeypatch,
                                                     tmp_path):
        # The probe runs BEFORE anything destructive, so there is nothing to
        # recover — and recovery must not run, or it would read a .skel-backup
        # this invocation never wrote into a home that never needed it.
        self._reroot(monkeypatch, tmp_path)
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        (home / ".bashrc").write_text("skel\n")
        stale = tmp_path / "var/lib/qdistro/silos/work/.skel-backup"
        stale.mkdir(parents=True)
        (stale / "from-an-older-attempt").write_text("stale\n")
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("filesystem df"))
        sm._SystemOps()._convert_home_to_subvolume("work", 2000)
        assert (home / ".bashrc").read_text() == "skel\n"
        assert not (home / "from-an-older-attempt").exists(), \
            "recovery ran before the point of no return and used a stale backup"

    def test_subvolume_create_timeout_leaves_a_usable_home(self, monkeypatch,
                                                          tmp_path, caplog):
        """The fallback's PROMISE is a usable home — assert the home.

        The conversion rmtree's the real home BEFORE creating its replacement,
        so for a failure in that window the old promise was simply false:
        CreateSilo reported success for a silo with no home directory at all and
        the skeleton stranded in the backup. (True for a plain non-zero exit
        too — this predates timeouts.) Asserting only "returned normally" is
        exactly the vacuous assertion that let it survive.
        """
        self._reroot(monkeypatch, tmp_path)
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        (home / ".bashrc").write_text("skel\n")
        (home / ".config").mkdir()
        (home / ".config" / "x.conf").write_text("cfg\n")
        # chown to a foreign uid is not permitted for an unprivileged runner;
        # the mode half of the hardening is still observable.
        monkeypatch.setattr(sm.os, "chown", lambda *a, **k: None)

        seen = []

        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            seen.append(argv)
            if "subvolume" in argv:
                raise sm.subprocess.TimeoutExpired(argv, kw.get("timeout"))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)

        with caplog.at_level("WARNING"):
            sm._SystemOps()._convert_home_to_subvolume("work", 2000)

        # Non-vacuous: we really reached the create and took the fallback.
        assert any("subvolume" in a for a in seen), seen
        assert any("btrfs subvolume conversion" in r.message
                   for r in caplog.records)
        # The actual requirement.
        assert home.is_dir(), "fallback left the silo with NO home directory"
        assert (home / ".bashrc").read_text() == "skel\n"
        assert (home / ".config" / "x.conf").read_text() == "cfg\n"
        assert home.stat().st_mode & 0o777 == 0o700, "home is not 0700"
        assert not (tmp_path / "var/lib/qdistro/silos/work/.skel-backup").exists()

    def test_a_partial_rmtree_still_triggers_recovery(self, monkeypatch,
                                                      tmp_path, caplog):
        # rmtree is not atomic: it can delete part of the home and then raise.
        # A recovery flag set AFTER it would skip the repair for a home that had
        # already been damaged.
        self._reroot(monkeypatch, tmp_path)
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        (home / ".bashrc").write_text("skel\n")
        monkeypatch.setattr(sm.os, "chown", lambda *a, **k: None)
        real_rmtree = sm.shutil.rmtree

        def half_rmtree(path, *a, **k):
            if str(path).endswith("/home/work"):
                (home / ".bashrc").unlink()
                raise OSError(5, "I/O error")
            return real_rmtree(path, *a, **k)
        monkeypatch.setattr(sm.shutil, "rmtree", half_rmtree)
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda argv, **kw: types.SimpleNamespace(
                returncode=0, stdout="", stderr=""))

        with caplog.at_level("WARNING"):
            sm._SystemOps()._convert_home_to_subvolume("work", 2000)
        assert (home / ".bashrc").read_text() == "skel\n", \
            "a partially removed home was not repaired"


class TestHomeRecoveryHardening:
    """_restore_plain_home's three separate promises, pinned separately.

    They are separate on purpose: an incomplete SKELETON is an accepted
    fallback, but an absent or unhardened HOME is not, and the two must not
    share a fate — a copy failure on one dotfile must never skip the chown and
    chmod that make the home the silo's and nobody else's.
    """

    def _ops(self):
        return sm._SystemOps()

    def test_hardening_survives_a_failed_skeleton_restore(self, monkeypatch,
                                                          tmp_path):
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        home.chmod(0o755)
        backup = tmp_path / "var/lib/qdistro/silos/work/.skel-backup"
        backup.mkdir(parents=True)
        (backup / ".bashrc").write_text("skel\n")
        chowned = []
        monkeypatch.setattr(sm.os, "chown",
                            lambda *a, **k: chowned.append(a[0]))
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(__import__("pathlib").Path(*a)).lstrip("/"))

        def boom(*a, **k):
            raise PermissionError(13, "nope")
        monkeypatch.setattr(sm.shutil, "copy2", boom)

        self._ops()._restore_plain_home(home, 2000)
        assert home.stat().st_mode & 0o777 == 0o700, \
            "a failed skeleton copy skipped the hardening"
        assert chowned, "a failed skeleton copy skipped the chown"

    def test_an_incomplete_restore_keeps_the_backup(self, monkeypatch, tmp_path):
        # Otherwise the only surviving copy of a file that was never restored
        # is deleted along with it.
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        backup = tmp_path / "var/lib/qdistro/silos/work/.skel-backup"
        backup.mkdir(parents=True)
        (backup / ".bashrc").write_text("skel\n")
        monkeypatch.setattr(sm.os, "chown", lambda *a, **k: None)
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(__import__("pathlib").Path(*a)).lstrip("/"))
        monkeypatch.setattr(sm.shutil, "copy2",
                            lambda *a, **k: (_ for _ in ()).throw(
                                PermissionError(13, "nope")))
        self._ops()._restore_plain_home(home, 2000)
        assert backup.exists(), "an incomplete restore deleted the backup"

    def test_a_complete_restore_removes_the_backup(self, monkeypatch, tmp_path):
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        backup = tmp_path / "var/lib/qdistro/silos/work/.skel-backup"
        backup.mkdir(parents=True)
        (backup / ".bashrc").write_text("skel\n")
        monkeypatch.setattr(sm.os, "chown", lambda *a, **k: None)
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(__import__("pathlib").Path(*a)).lstrip("/"))
        self._ops()._restore_plain_home(home, 2000)
        assert (home / ".bashrc").read_text() == "skel\n"
        assert not backup.exists()

    def test_a_missing_home_that_cannot_be_recreated_raises(self, monkeypatch,
                                                            tmp_path):
        # An absent home is NOT an accepted fallback: better to fail the create
        # than to report success for a silo that cannot log in.
        # A file where the home should be: mkdir cannot create the directory.
        (tmp_path / "home").mkdir()
        home = tmp_path / "home" / "work"
        home.write_text("not a directory\n")
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(__import__("pathlib").Path(*a)).lstrip("/"))
        with pytest.raises(OSError):
            self._ops()._restore_plain_home(home, 2000)

    def test_unhardenable_home_raises(self, monkeypatch, tmp_path):
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(__import__("pathlib").Path(*a)).lstrip("/"))
        monkeypatch.setattr(sm.os, "chown",
                            lambda *a, **k: (_ for _ in ()).throw(
                                PermissionError(13, "nope")))
        with pytest.raises(OSError):
            self._ops()._restore_plain_home(home, 2000)


class TestMergeMissingIsRecursiveAndDoesNotDereference:
    def test_a_partially_restored_directory_is_completed(self, tmp_path):
        # "The destination exists" is not "its subtree was restored": the
        # interrupted restore may have made .config and copied one of its files.
        src, dst = tmp_path / "b", tmp_path / "h"
        (src / ".config").mkdir(parents=True)
        (src / ".config" / "kept").write_text("a\n")
        (src / ".config" / "missing").write_text("b\n")
        (dst / ".config").mkdir(parents=True)
        (dst / ".config" / "kept").write_text("a\n")
        # The missing descendant IS restored...
        result = sm._SystemOps._merge_missing(src, dst)
        assert (dst / ".config" / "missing").read_text() == "b\n"
        # ...but the result is incomplete, because `kept` was already there and
        # an interrupted copy can leave a TRUNCATED file. We cannot establish
        # it matches the backup, so the backup must survive.
        assert result is False

    def test_a_present_but_unverifiable_file_keeps_the_backup(self, tmp_path):
        src, dst = tmp_path / "b", tmp_path / "h"
        src.mkdir(); dst.mkdir()
        (src / "f").write_text("the whole file\n")
        (dst / "f").write_text("the who")        # truncated by an interrupted copy
        assert sm._SystemOps._merge_missing(src, dst) is False
        assert (dst / "f").read_text() == "the who", "existing data overwritten"

    def test_an_empty_destination_restores_completely(self, tmp_path):
        # The normal case — the home was just recreated — must still come out
        # complete, or the conservative rule would keep every backup forever.
        src, dst = tmp_path / "b", tmp_path / "h"
        (src / ".config").mkdir(parents=True)
        (src / ".config" / "x").write_text("x\n")
        (src / ".bashrc").write_text("b\n")
        (src / "link").symlink_to("/etc/hostname")
        dst.mkdir()
        assert sm._SystemOps._merge_missing(src, dst) is True
        assert (dst / ".config" / "x").read_text() == "x\n"
        assert (dst / "link").is_symlink()

    def test_a_destination_directory_symlink_is_refused(self, tmp_path):
        # mkdir(exist_ok=True) accepts a symlink-to-directory, and the recursion
        # would then write the backup through it, outside the home.
        outside = tmp_path / "outside"
        outside.mkdir()
        src, dst = tmp_path / "b", tmp_path / "h"
        (src / ".config").mkdir(parents=True)
        (src / ".config" / "x").write_text("x\n")
        dst.mkdir()
        (dst / ".config").symlink_to(outside)
        assert sm._SystemOps._merge_missing(src, dst) is False
        assert not (outside / "x").exists(), "wrote through a destination link"

    def test_a_dangling_destination_symlink_is_refused(self, tmp_path):
        # exists() is False for a dangling link, and copy2's
        # follow_symlinks=False governs the SOURCE, not the destination open —
        # so the copy would follow it and create the link's target.
        src, dst = tmp_path / "b", tmp_path / "h"
        src.mkdir(); dst.mkdir()
        (src / "f").write_text("secret\n")
        (dst / "f").symlink_to(tmp_path / "outside-target")
        assert sm._SystemOps._merge_missing(src, dst) is False
        assert not (tmp_path / "outside-target").exists(), \
            "followed a dangling destination link"

    def test_existing_home_data_is_never_overwritten(self, tmp_path):
        src, dst = tmp_path / "b", tmp_path / "h"
        src.mkdir(); dst.mkdir()
        (src / "f").write_text("from-backup\n")
        (dst / "f").write_text("the-user-file\n")
        sm._SystemOps._merge_missing(src, dst)
        assert (dst / "f").read_text() == "the-user-file\n"

    def test_a_directory_symlink_is_not_dereferenced(self, tmp_path):
        # is_dir() follows symlinks, and copytree(symlinks=True) preserves links
        # found INSIDE a tree but still follows the root it is handed. So a
        # top-level directory symlink would be materialised as a real directory
        # in the silo home — and then chowned to the silo uid, handing it a copy
        # of whatever the link pointed at.
        private = tmp_path / "private"
        private.mkdir()
        (private / "secret").write_text("s3cret\n")
        src, dst = tmp_path / "b", tmp_path / "h"
        src.mkdir(); dst.mkdir()
        (src / "link").symlink_to(private)
        assert sm._SystemOps._merge_missing(src, dst) is True
        assert (dst / "link").is_symlink(), "the link was dereferenced"
        assert not (dst / "link").resolve().joinpath("secret").is_file() \
            or (dst / "link").readlink() == private

    def test_a_failure_is_reported_as_incomplete(self, tmp_path, monkeypatch):
        src, dst = tmp_path / "b", tmp_path / "h"
        src.mkdir(); dst.mkdir()
        (src / "f").write_text("x\n")
        monkeypatch.setattr(sm.shutil, "copy2",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(5, "io")))
        assert sm._SystemOps._merge_missing(src, dst) is False

    def test_a_symlinked_home_root_is_refused(self, tmp_path):
        # mkdir(exist_ok=True) accepts a symlink-to-directory, and the chown +
        # chmod that follow would then retarget whatever it points at.
        outside = tmp_path / "outside"
        outside.mkdir()
        (tmp_path / "home").mkdir()
        home = tmp_path / "home" / "work"
        home.symlink_to(outside)
        # NB assert on the MESSAGE, not pytest's match=: tmp_path is named
        # after the test ("..._a_symlinked_home_root_is_0"), so match="symlink"
        # is satisfied by the path echoed in ANY OSError — including the
        # unrelated EPERM an unprivileged chown raises. That false pass hid a
        # removed guard until mutation testing caught it.
        with pytest.raises(OSError) as ei:
            sm._SystemOps()._restore_plain_home(home, 2000)
        assert "refusing to restore into" in str(ei.value), str(ei.value)

    def test_a_symlinked_backup_root_is_refused(self, monkeypatch, tmp_path):
        # is_dir() follows a link, so an unvalidated backup root would be read
        # from wherever it pointed. Refused, and the backup is kept.
        import pathlib as _pl
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(_pl.Path(*a)).lstrip("/"))
        monkeypatch.setattr(sm.os, "chown", lambda *a, **k: None)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "planted").write_text("x\n")
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        backup = tmp_path / "var/lib/qdistro/silos/work/.skel-backup"
        backup.parent.mkdir(parents=True)
        backup.symlink_to(elsewhere)
        sm._SystemOps()._restore_plain_home(home, 2000)
        assert not (home / "planted").exists(), "read through a linked backup"
        assert backup.is_symlink(), "an incomplete restore removed the backup"


class TestSuccessfulConversionDoesNotChownThroughLinks:
    def test_a_preserved_directory_symlink_is_chowned_no_follow(self, monkeypatch,
                                                                tmp_path):
        """os.walk lists a preserved directory symlink in `dirs`.

        A following chown would then hand the silo uid ownership of whatever it
        points at — an external, possibly root-owned directory — while the home
        itself looks fine. The fallback already used follow_symlinks=False; this
        pins the successful path, which did not.
        """
        import pathlib as _pl
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(_pl.Path(*a)).lstrip("/"))
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "planted").write_text("not the silo's\n")
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        (home / "link").symlink_to(outside)
        # The conversion must genuinely SUCCEED for this test to reach the
        # happy-path chown at all: a fake that no-ops the rmtree makes the
        # restore raise FileExistsError and diverts into the recovery handler,
        # whose chown was already no-follow — so the test would pass while
        # asserting nothing. Let rmtree really run and have the fake
        # `subvolume create` recreate the directory.
        def fake_run(argv, **kw):
            if "subvolume" in [str(a) for a in argv]:
                home.mkdir(parents=True, exist_ok=True)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        chowns = []
        monkeypatch.setattr(
            sm.os, "chown",
            lambda path, u, g, **kw: chowns.append((str(path),
                                                    kw.get("follow_symlinks"))))
        sm._SystemOps()._convert_home_to_subvolume("work", 2000)
        assert (home / "link").is_symlink(), \
            "the conversion did not complete; this test proves nothing"
        through = [c for c in chowns
                   if c[0].endswith("/link") and c[1] is not False]
        assert not through, f"chowned through a directory symlink: {through}"
        assert (outside / "planted").exists(), "the link target was disturbed"


class TestAnUnacknowledgedStopKeepsTheSiloActive:
    """A snapshot cannot settle a stop that PID 1 may never have accepted.

    This is the hole round 4 left: the start handler correctly refused to
    resolve the uncertainty, and then handed it to a stop path that resolved it
    anyway. A start job WAITING on a dependency has not begun executing, so the
    unit reads `inactive` and its container does not exist YET — byte-identical
    to a clean stop, and it launches minutes later. So an unacknowledged stop
    must keep the silo Active no matter how good the snapshot looks.
    """

    def test_systemctl_stop_reports_completion(self, monkeypatch):
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda argv, **kw: types.SimpleNamespace(
                returncode=0, stdout="", stderr=""))
        assert sm._SystemOps().systemctl_stop("u.service") is True

    def test_systemctl_stop_reports_a_timeout_as_not_completed(self, monkeypatch):
        monkeypatch.setattr(sm.subprocess, "run", _timeout_on("stop"))
        assert sm._SystemOps().systemctl_stop("u.service") is False

    def test_an_arbitrary_nonzero_exit_is_NOT_completion(self, monkeypatch):
        """Process completion is not stop completion.

        An earlier round of this change pinned the opposite, on the reasoning
        that check=False means "already stopped is not a failure". That was
        wrong: `systemctl` exiting non-zero can mean PID 1 never accepted the
        transaction — an irreversible conflicting job cannot be replaced, so
        the stop is never enqueued — and a bus failure exits unsuccessfully
        having cancelled nothing. The queued start then runs later, and the
        inactive/absent snapshot that follows looks exactly like a clean stop.
        """
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda argv, **kw: types.SimpleNamespace(
                returncode=1, stdout="", stderr="Failed to stop u.service.\n"))
        assert sm._SystemOps().systemctl_stop("u.service") is False

    def test_an_unloaded_unit_is_ALSO_not_completion(self, monkeypatch):
        """There is deliberately no "unit not loaded" exception.

        An earlier round had one, reasoning that a queued job implies a loaded
        unit. Codex broke it twice over: the stderr match also catches a
        failure to reach the BUS at all ("Failed to connect to system scope bus
        ...: No such file or directory"), which says nothing about PID 1's
        existing jobs; and StopUnit LOADS the unit it is given, so a
        never-loaded launcher stops normally and needed no exception anyway.
        Requiring rc == 0 is the only rule that is actually sound.
        """
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda argv, **kw: types.SimpleNamespace(
                returncode=5, stdout="",
                stderr="Failed to stop u.service: Unit u.service not loaded.\n"))
        assert sm._SystemOps().systemctl_stop("u.service") is False

    def test_a_bus_connection_failure_is_not_completion(self, monkeypatch):
        # The specific string that made the old exception unsound. Pinned so it
        # cannot be reintroduced by pattern-matching on "no such file".
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda argv, **kw: types.SimpleNamespace(
                returncode=1, stdout="",
                stderr="Failed to connect to system scope bus via local "
                       "transport: No such file or directory\n"))
        assert sm._SystemOps().systemctl_stop("u.service") is False

    def test_a_clean_exit_is_completion(self, monkeypatch):
        monkeypatch.setattr(
            sm.subprocess, "run",
            lambda argv, **kw: types.SimpleNamespace(
                returncode=0, stdout="", stderr=""))
        assert sm._SystemOps().systemctl_stop("u.service") is True

    def _tier2(self, tmp_path, stop_done, survived):
        from test_session_manager import _LAUNCH, _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("browser1", sm.TIER2_LAUNCH_OWNER_UID,
                     kind=sm.KIND_TIER2_TEMPLATE, launch=dict(_LAUNCH))
        store.start("browser1")
        ops.systemctl_stop_unacknowledged = not stop_done
        ops.tier2_stop_fails = survived
        return store

    def test_an_unacknowledged_stop_keeps_it_active(self, tmp_path):
        # The exact sequence from the review: stop times out, yet the unit reads
        # inactive and the container is absent because the start never ran.
        store = self._tier2(tmp_path, stop_done=False, survived=False)
        with pytest.raises(sm.SessionError, match="not acknowledged"):
            store.stop("browser1", 0)
        assert store.get("browser1").state == sm.State.ACTIVE
        with pytest.raises(sm.SessionError):
            store.delete("browser1")

    def test_a_surviving_container_still_keeps_it_active(self, tmp_path):
        store = self._tier2(tmp_path, stop_done=True, survived=True)
        with pytest.raises(sm.SessionError, match="did not take effect"):
            store.stop("browser1", 0)
        assert store.get("browser1").state == sm.State.ACTIVE

    def _tier3(self, tmp_path, stop_done):
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        store.start("work")
        ops.systemctl_stop_unacknowledged = not stop_done
        return store

    def test_an_ordinary_silo_also_stays_active(self, tmp_path):
        # Same hazard, same answer: an empty cgroup is not proof when the stop
        # was never acknowledged — a queued start leaves it empty simply
        # because nothing has launched yet.
        store = self._tier3(tmp_path, stop_done=False)
        with pytest.raises(sm.SessionError, match="not acknowledged"):
            store.stop("work", 0)
        assert store.get("work").state == sm.State.ACTIVE
        with pytest.raises(sm.SessionError):
            store.delete("work")

    def test_an_ordinary_silo_with_an_acknowledged_stop_reaches_stopped(
            self, tmp_path):
        store = self._tier3(tmp_path, stop_done=True)
        store.stop("work", 0)
        assert store.get("work").state == sm.State.STOPPED

    def test_a_teardown_error_cannot_erase_the_uncertainty(self, tmp_path):
        """Raising an error is not fail-closed; the PERSISTED state is.

        The unresolved-stop gate sits after the teardown, so the broad
        `except` handlers run first — and they used to force STOPPED
        unconditionally. StopSilo would then raise, look like a failure, and
        still leave a row DeleteSilo accepts. Codex reproduced exactly this
        with a kill_pids OSError.
        """
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        store.start("work")
        ops.systemctl_stop_unacknowledged = True
        ops.cgroup_pids = lambda name: [4242]
        ops.kill_pids = lambda pids, sig: (_ for _ in ()).throw(
            OSError(1, "Operation not permitted"))
        with pytest.raises(sm.SessionError):
            store.stop("work", 0)
        assert store.get("work").state == sm.State.ACTIVE, \
            "a teardown error erased the unresolved cancellation"
        with pytest.raises(sm.SessionError):
            store.delete("work")

    def test_a_teardown_error_after_an_ACKNOWLEDGED_stop_still_reaches_stopped(
            self, tmp_path):
        # The converse: the handlers must keep forcing STOPPED when the stop
        # WAS acknowledged, or a teardown hiccup would wedge every silo Active.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        store.start("work")
        ops.cgroup_pids = lambda name: [4242]
        ops.kill_pids = lambda pids, sig: (_ for _ in ()).throw(
            OSError(1, "Operation not permitted"))
        with pytest.raises(sm.SessionError):
            store.stop("work", 0)
        assert store.get("work").state == sm.State.STOPPED

    def test_a_raising_systemctl_stop_leaves_it_active(self, tmp_path):
        # Nothing ever assigned stop_done here, so this pins that its
        # conservative initial value is False rather than True.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000)
        store.start("work")
        ops.systemctl_stop = lambda unit, **kw: (_ for _ in ()).throw(
            OSError(24, "Too many open files"))
        with pytest.raises(sm.SessionError):
            store.stop("work", 0)
        assert store.get("work").state == sm.State.ACTIVE

    def test_the_egress_survives_an_unresolved_stop(self, tmp_path):
        # Same rationale as start()'s StartNotCancelled path: a queued start can
        # still launch, and stripping its network out from under it strands a
        # live workload on a half-removed netns.
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000, egress="none")
        store.start("work")
        ops.egress_calls.clear()
        ops.systemctl_stop_unacknowledged = True
        with pytest.raises(sm.SessionError, match="not acknowledged"):
            store.stop("work", 0)
        assert not any(c[0] == "netns_remove" for c in ops.egress_calls), \
            ops.egress_calls

    def test_the_egress_is_torn_down_on_a_resolved_stop(self, tmp_path):
        from test_session_manager import _FakeOps
        ops = _FakeOps()
        store = sm._SiloStore(ops, config_path=tmp_path / "silos.yaml")
        store.create("work", 2000, egress="none")
        store.start("work")
        ops.egress_calls.clear()
        store.stop("work", 0)
        assert any(c[0] == "netns_remove" for c in ops.egress_calls), \
            ops.egress_calls

    def test_a_completed_verified_stop_reaches_stopped(self, tmp_path):
        # The converse, so "stay Active" cannot be over-applied: a stop that
        # completed AND found the workload gone must still reach STOPPED, or no
        # tier-2 silo could ever be deleted.
        store = self._tier2(tmp_path, stop_done=True, survived=False)
        store.stop("browser1", 0)
        assert store.get("browser1").state == sm.State.STOPPED


class TestRecoveryChownDoesNotFollowLinks:
    def test_the_fallback_path_chowns_no_follow(self, monkeypatch, tmp_path):
        """The recovery path's descendant chown, not the successful one.

        Codex's round-4 mutation flipped `follow_symlinks=False` to True in
        _restore_plain_home and passed all 64 tests: the successful-conversion
        test covers the other loop. A preserved directory link here would hand
        the silo uid ownership of an external target.
        """
        import pathlib as _pl
        monkeypatch.setattr(
            sm, "Path",
            lambda *a: tmp_path / str(_pl.Path(*a)).lstrip("/"))
        outside = tmp_path / "outside"
        outside.mkdir()
        home = tmp_path / "home" / "work"
        home.mkdir(parents=True)
        backup = tmp_path / "var/lib/qdistro/silos/work/.skel-backup"
        backup.mkdir(parents=True)
        (backup / "link").symlink_to(outside)
        chowns = []
        monkeypatch.setattr(
            sm.os, "chown",
            lambda path, u, g, **kw: chowns.append((str(path),
                                                    kw.get("follow_symlinks"))))
        sm._SystemOps()._restore_plain_home(home, 2000)
        assert (home / "link").is_symlink(), \
            "the link was not restored; this test proves nothing"
        through = [c for c in chowns
                   if c[0].endswith("/link") and c[1] is not False]
        assert not through, f"chowned through a directory symlink: {through}"


class TestLivenessEvidenceIsBoundToTheSiloAsked_About:
    """The container check must be about THIS silo.

    Codex's round-5 mutation changed `TIER2_CONTAINER_FMT.format(name=name)` to
    a literal and passed all 73 tests: every fake answered the same regardless
    of the argv it was handed, so "absent" was never tied to the silo under
    test. A liveness check that asks about the wrong container is a fail-open
    that looks exactly like a working one.
    """

    def _podman_inventory(self, monkeypatch, present, active=()):
        """Both answers depend on the EXACT name in the argv.

        Codex's round-6 mutation pointed the `is-active` query at a literal
        instead of the silo's unit and passed all 83 tests, because the
        systemctl answers were name-independent even after the container ones
        were bound. Every liveness answer here is per-name.
        """
        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "is-active" in argv:
                return types.SimpleNamespace(
                    returncode=0,
                    stdout=("active\n" if argv[-1] in active else "inactive\n"),
                    stderr="")
            if "exists" in argv:
                # `podman container exists`: rc 0 present, rc 1 absent.
                return types.SimpleNamespace(
                    returncode=0 if argv[-1] in present else 1,
                    stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)

    def _owned_inventory(self, monkeypatch, by_owner):
        """Answers depend on the `runuser -u <owner>` principal AND the name.

        The rootless container is ADMIN's. Root's podman has its own, empty
        store, so a negative answer from root is not evidence about admin's
        container — it is a fail-open that looks exactly like a clean stop.
        """
        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "is-active" in argv:
                return types.SimpleNamespace(
                    returncode=0, stdout="inactive\n", stderr="")
            if "exists" in argv:
                owner = argv[argv.index("-u") + 1] if "-u" in argv else None
                return types.SimpleNamespace(
                    returncode=0 if argv[-1] in by_owner.get(owner, ())
                    else 1, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)

    def test_the_container_is_looked_for_in_admins_store(self, monkeypatch):
        live = sm.TIER2_CONTAINER_FMT.format(name="work")
        self._owned_inventory(monkeypatch, {sm.ADMIN_USER_NAME: {live}})
        assert sm._SystemOps().tier2_silo_running("work") is True

    def test_a_container_only_root_can_see_is_not_this_silos(self, monkeypatch):
        # The converse, so the check cannot be "always True": something in
        # root's store must not keep an admin-owned silo Active.
        live = sm.TIER2_CONTAINER_FMT.format(name="work")
        self._owned_inventory(monkeypatch, {"root": {live}})
        assert sm._SystemOps().tier2_silo_running("work") is False

    def test_the_exists_query_drops_to_admin(self, monkeypatch):
        asked = []

        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "is-active" in argv:
                return types.SimpleNamespace(
                    returncode=0, stdout="inactive\n", stderr="")
            if "exists" in argv:
                asked.append(argv)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        sm._SystemOps().tier2_silo_running("work")
        assert asked == [[
            "runuser", "-u", sm.ADMIN_USER_NAME, "--",
            "podman", "container", "exists",
            sm.TIER2_CONTAINER_FMT.format(name="work")]], asked

    def test_a_live_launcher_for_THIS_silo_is_seen(self, monkeypatch):
        unit = sm.TIER2_SILO_LAUNCHER_FMT.format(name="work")
        self._podman_inventory(monkeypatch, present=set(), active={unit})
        assert sm._SystemOps().tier2_silo_running("work") is True

    def test_another_silos_live_launcher_does_not_count(self, monkeypatch):
        other = sm.TIER2_SILO_LAUNCHER_FMT.format(name="other")
        self._podman_inventory(monkeypatch, present=set(), active={other})
        assert sm._SystemOps().tier2_silo_running("work") is False

    def test_the_is_active_query_names_this_silos_unit(self, monkeypatch):
        asked = []

        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "is-active" in argv:
                asked.append(argv[-1])
                return types.SimpleNamespace(
                    returncode=0, stdout="inactive\n", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        sm._SystemOps().tier2_silo_running("work")
        assert asked == [sm.TIER2_SILO_LAUNCHER_FMT.format(name="work")], asked

    def test_a_surviving_container_for_THIS_silo_is_seen(self, monkeypatch):
        live = sm.TIER2_CONTAINER_FMT.format(name="work")
        self._podman_inventory(monkeypatch, {live})
        assert sm._SystemOps().tier2_silo_running("work") is True

    def test_another_silos_container_does_not_count(self, monkeypatch):
        # The converse, so the check cannot be "always True": a container
        # belonging to a DIFFERENT silo must not keep this one Active.
        other = sm.TIER2_CONTAINER_FMT.format(name="other")
        self._podman_inventory(monkeypatch, {other})
        assert sm._SystemOps().tier2_silo_running("work") is False

    def test_the_query_names_this_silos_container(self, monkeypatch):
        asked = []

        def fake_run(argv, **kw):
            argv = [str(a) for a in argv]
            if "is-active" in argv:
                return types.SimpleNamespace(
                    returncode=0, stdout="inactive\n", stderr="")
            if "exists" in argv:
                asked.append(argv[-1])
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        monkeypatch.setattr(sm.subprocess, "run", fake_run)
        sm._SystemOps().tier2_silo_running("work")
        assert asked == [sm.TIER2_CONTAINER_FMT.format(name="work")], asked

