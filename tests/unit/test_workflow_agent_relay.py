"""Unit tests for the zero-coordination ssh-agent relay (security-critical).

The relay is the missing piece that makes a *pure* ``process_spawn(git)``
signing flow work: the dev runs plain ``git commit -S`` against a FIXED
per-user agent path; the relay holds the connection until the engine
publishes this run's per-run agent, then transparently byte-pumps to it.

These tests prove, against a REAL ssh-agent (skipped if ssh tooling is
absent):

  - connect-BEFORE-publish blocks then succeeds the moment set_target fires
    (the central race the relay is supposed to resolve);
  - connect-with-target-already-set relays immediately;
  - clear_target re-arms fail-closed (a later connection gets no agent);
  - a connection that never gets a target fails closed within connect_wait;
  - the run-aware ChannelRelayRegistrar adapts the engine's
    (run_id, name, value|None) callback, ignores non-allowlisted names, and
    only lets the OWNING run's scrub re-arm fail-closed;
  - end-to-end through the WorkflowEngine: a run's published SSH_AUTH_SOCK
    reaches the relay, a client on the fixed path lists the key while the run
    is live, and after the run the relay fails closed and the per-run socket
    is gone.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

from agent_relay import (  # noqa: E402
    ChannelRelayRegistrar,
    SshAgentRelay,
    _proc_starttime,
    build_relay_registrar,
)
from workflow_schema import (  # noqa: E402
    StepDef, StepType, TriggerDef, TriggerType, WorkflowDef, RunState,
)
from workflow_engine import WorkflowEngine  # noqa: E402
from audit_logger import WorkflowAuditLogger  # noqa: E402

_HAVE_SSH = all(shutil.which(b) for b in ("ssh-agent", "ssh-add", "ssh-keygen"))


# ----------------------------------------------------------------------
# Real-agent helpers
# ----------------------------------------------------------------------


def _gen_key(tmp_path) -> bytes:
    kp = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                    "-f", str(kp)], check=True)
    return kp.read_bytes()


class _RealAgent:
    """A real per-run ssh-agent holding one key; ``sock`` is its path."""

    def __init__(self, tmp_path, key: bytes):
        self._dir = tmp_path / "agent"
        self._dir.mkdir()
        self.sock = str(self._dir / "agent.sock")
        out = subprocess.run(["ssh-agent", "-a", self.sock],
                             capture_output=True, text=True, check=True)
        self._pid = None
        for line in out.stdout.splitlines():
            if line.startswith("SSH_AGENT_PID="):
                self._pid = int(line.split("=", 1)[1].split(";", 1)[0])
        # Feed the key over stdin (like the real SshAgentDelivery) so we
        # don't trip ssh-add's "UNPROTECTED PRIVATE KEY FILE" perms check.
        if key and not key.endswith(b"\n"):
            key = key + b"\n"
        subprocess.run(["ssh-add", "-"], input=key,
                       env=dict(os.environ, SSH_AUTH_SOCK=self.sock),
                       capture_output=True, check=True)

    def kill(self):
        if self._pid:
            try:
                os.kill(self._pid, 15)
            except OSError:
                pass


def _ssh_add_list(sock: str, timeout: float = 8.0):
    """Run `ssh-add -l` against ``sock``; return the CompletedProcess."""
    return subprocess.run(
        ["ssh-add", "-l"], env=dict(os.environ, SSH_AUTH_SOCK=sock),
        capture_output=True, text=True, timeout=timeout)


def _blocked_conn_count() -> int:
    """Number of live relay serve threads process-wide. Snapshot this BEFORE
    connecting so the wait below keys off an *increase*, not an absolute count
    (a serve thread left alive by a prior test must not satisfy the signal)."""
    return sum(1 for t in threading.enumerate()
               if t.name == "ssh-agent-relay-conn" and t.is_alive())


def _wait_for_blocked_conn(n: int = 1, *, baseline: int = 0,
                           timeout: float = 8.0) -> None:
    """Block until ``n`` NEW front connections (above ``baseline``) are parked
    in the relay's serve loop (the deterministic readiness signal that replaces
    a fixed sleep).

    When a client connects to the fixed front socket the relay's
    ``_accept_loop`` spawns a per-connection serve thread named
    ``ssh-agent-relay-conn`` (see ``SshAgentRelay._accept_loop``). That
    thread exists exactly once the connection has been accepted and is now
    waiting (bounded) for ``set_target`` — precisely the "connected but
    blocked, no target yet" state the old ``time.sleep`` was approximating.
    Polling ``threading.enumerate()`` for that thread is race-free and
    observes the real relay state instead of guessing a wall-clock delay.

    Fails loudly (AssertionError) if the connection never reaches the relay
    within ``timeout`` — a real bug, not a flake to be papered over.
    """
    target = baseline + n
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        serving = _blocked_conn_count()
        if serving >= target:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"relay never accepted {n} new connection(s) within {timeout}s "
        f"(baseline {baseline}, saw {serving} ssh-agent-relay-conn thread(s))")


# ----------------------------------------------------------------------
# SshAgentRelay (transport)
# ----------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.needs_ssh
@pytest.mark.skipif(not _HAVE_SSH, reason="ssh tooling not available")
class TestSshAgentRelay:
    def test_target_already_set_relays_immediately(self, tmp_path):
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        relay = SshAgentRelay(str(tmp_path / "front.sock"))
        relay.start()
        try:
            relay.set_target(agent.sock)
            r = _ssh_add_list(relay.front_path)
            assert r.returncode == 0  # agent had the key
            assert "ED25519" in r.stdout
        finally:
            relay.stop()
            agent.kill()

    def test_connect_before_publish_blocks_then_succeeds(self, tmp_path):
        # THE central race: git connects to the fixed path BEFORE the per-run
        # agent exists. The relay must hold the connection, not fail, and
        # complete once set_target lands.
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        relay = SshAgentRelay(str(tmp_path / "front.sock"), connect_wait_s=5.0)
        relay.start()
        result = {}

        def client():
            result["proc"] = _ssh_add_list(relay.front_path)

        try:
            _base = _blocked_conn_count()
            t = threading.Thread(target=client, daemon=True)
            t.start()
            # Readiness signal (replaces a fixed time.sleep(1.0)): wait until
            # the relay has actually accepted the connection and parked it in
            # the serve loop waiting for a target — the exact "connected but
            # blocked, no target yet" state this test needs before publishing.
            _wait_for_blocked_conn(1, baseline=_base)
            assert t.is_alive(), "client should still be blocked, no target yet"
            # Now publish — simulates the engine's deliver_secret completing.
            relay.set_target(agent.sock)
            t.join(timeout=8.0)
            assert not t.is_alive(), "client should have completed after publish"
            assert result["proc"].returncode == 0
            assert "ED25519" in result["proc"].stdout
        finally:
            relay.stop()
            agent.kill()

    def test_no_target_fails_closed(self, tmp_path):
        # No target ever set -> the relay closes the connection at the
        # deadline; ssh-add sees a dead agent (non-zero), never an unsigned
        # success against the wrong agent.
        relay = SshAgentRelay(str(tmp_path / "front.sock"), connect_wait_s=0.5)
        relay.start()
        try:
            r = _ssh_add_list(relay.front_path, timeout=8.0)
            assert r.returncode != 0  # fail closed
        finally:
            relay.stop()

    def test_clear_target_rearms_fail_closed(self, tmp_path):
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        relay = SshAgentRelay(str(tmp_path / "front.sock"), connect_wait_s=0.5)
        relay.start()
        try:
            relay.set_target(agent.sock)
            assert _ssh_add_list(relay.front_path).returncode == 0
            # Scrub the run -> relay must no longer relay.
            relay.clear_target()
            assert _ssh_add_list(relay.front_path, timeout=8.0).returncode != 0
        finally:
            relay.stop()
            agent.kill()

    def test_front_socket_is_owner_only(self, tmp_path):
        relay = SshAgentRelay(str(tmp_path / "front.sock"))
        relay.start()
        try:
            mode = os.stat(relay.front_path).st_mode & 0o777
            assert mode == 0o600, oct(mode)
        finally:
            relay.stop()

    def test_stop_unlinks_socket_and_winds_down(self, tmp_path):
        # Clean shutdown must remove the on-disk socket file and join the
        # accept thread (so a clean exit leaves nothing bound or stale).
        front = str(tmp_path / "front.sock")
        relay = SshAgentRelay(front)
        relay.start()
        assert os.path.exists(front)
        accept_thread = relay._accept_thread
        relay.stop()
        assert not os.path.exists(front)
        assert accept_thread is not None and not accept_thread.is_alive()

    def test_peer_in_owner_tree_relays(self, tmp_path):
        # Run-binding: when the target carries an owner pid that IS the
        # connecting peer (or its ancestor), the relay pumps. The test client
        # runs in this process via ssh-add as a subprocess; its parent is this
        # pytest process, so owner_pid=os.getpid() makes the client a
        # descendant -> allowed.
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        relay = SshAgentRelay(str(tmp_path / "front.sock"))
        relay.start()
        try:
            relay.set_target(agent.sock, owner_pid=os.getpid(),
                             owner_pid_starttime=_proc_starttime(os.getpid()))
            r = _ssh_add_list(relay.front_path)
            assert r.returncode == 0, r.stderr
            assert "ED25519" in r.stdout
        finally:
            relay.stop()
            agent.kill()

    def test_peer_not_in_owner_tree_fails_closed(self, tmp_path):
        # THE blocker fix: a live target owned by run A must NOT serve a peer
        # outside A's process tree, even at the same uid. We set owner_pid to a
        # live but unrelated process (a sleep we spawn) that is NOT an ancestor
        # of the ssh-add client; the relay must refuse and the client fails.
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        other = subprocess.Popen([sys.executable, "-c",
                                  "import time;time.sleep(30)"])
        relay = SshAgentRelay(str(tmp_path / "front.sock"), connect_wait_s=0.5)
        relay.start()
        try:
            relay.set_target(agent.sock, owner_pid=other.pid,
                             owner_pid_starttime=_proc_starttime(other.pid))
            r = _ssh_add_list(relay.front_path, timeout=8.0)
            assert r.returncode != 0, "peer outside owner tree must fail closed"
        finally:
            relay.stop()
            agent.kill()
            other.terminate()
            try:
                other.wait(timeout=5)
            except Exception:
                other.kill()

    def test_owner_pid_without_anchor_fails_closed(self, tmp_path):
        # Hardening: if a run supplies an owner pid but NO starttime anchor, the
        # relay must refuse rather than fall back to an un-anchored ancestry
        # walk (an un-anchored pid can't be told from a recycled one). Even
        # though the peer (this process tree) would otherwise match, the
        # missing anchor forces fail-closed.
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        relay = SshAgentRelay(str(tmp_path / "front.sock"), connect_wait_s=0.5)
        relay.start()
        try:
            relay.set_target(agent.sock, owner_pid=os.getpid(),
                             owner_pid_starttime=None)  # no anchor
            r = _ssh_add_list(relay.front_path, timeout=8.0)
            assert r.returncode != 0, "missing pid anchor must fail closed"
        finally:
            relay.stop()
            agent.kill()

    def test_owner_pid_reuse_anchor_fails_closed(self, tmp_path):
        # If the owning git has exited and its pid was recycled, the captured
        # starttime no longer matches -> the binding is stale -> fail closed,
        # even if the connecting peer happens to match the (reused) pid number.
        agent = _RealAgent(tmp_path, _gen_key(tmp_path))
        relay = SshAgentRelay(str(tmp_path / "front.sock"), connect_wait_s=0.5)
        relay.start()
        try:
            # owner_pid = our own pid, but a deliberately WRONG starttime.
            relay.set_target(agent.sock, owner_pid=os.getpid(),
                             owner_pid_starttime=1)  # bogus anchor
            r = _ssh_add_list(relay.front_path, timeout=8.0)
            assert r.returncode != 0, "stale pid-reuse anchor must fail closed"
        finally:
            relay.stop()
            agent.kill()

    def test_stop_while_client_blocked_does_not_hang(self, tmp_path):
        # A connection parked waiting for a target must not wedge stop():
        # the serve loop observes _stop and returns, and stop() is bounded.
        front = str(tmp_path / "front.sock")
        relay = SshAgentRelay(front, connect_wait_s=30.0)
        relay.start()
        blocked = {}

        def client():
            blocked["proc"] = _ssh_add_list(front, timeout=20.0)

        _base = _blocked_conn_count()
        t = threading.Thread(target=client, daemon=True)
        t.start()
        # Readiness signal (replaces a fixed time.sleep(1.0)): wait until the
        # connection is parked in the serve loop with no target, so stop() is
        # genuinely racing a blocked connection (the case under test).
        _wait_for_blocked_conn(1, baseline=_base)
        started = time.monotonic()
        relay.stop()
        assert time.monotonic() - started < 5.0, "stop() hung on a blocked conn"
        # The client's connection is dropped -> ssh-add fails closed.
        t.join(timeout=10.0)
        assert not t.is_alive()
        assert blocked["proc"].returncode != 0


# ----------------------------------------------------------------------
# ChannelRelayRegistrar (engine glue, no ssh needed)
# ----------------------------------------------------------------------


class _FakeRelay:
    def __init__(self):
        self.target = None
        self.owner_pid = None
        self.history = []

    def set_target(self, path, *, owner_pid=None, owner_pid_starttime=None):
        self.target = path
        self.owner_pid = owner_pid
        self.history.append(("set", path))

    def clear_target(self):
        self.target = None
        self.owner_pid = None
        self.history.append(("clear", None))


class TestChannelRelayRegistrar:
    def test_publish_sets_target(self):
        relay = _FakeRelay()
        reg = ChannelRelayRegistrar(relay)
        reg("run-1", "SSH_AUTH_SOCK", "/run/a.sock")
        assert relay.target == "/run/a.sock"

    def test_scrub_by_owner_clears(self):
        relay = _FakeRelay()
        reg = ChannelRelayRegistrar(relay)
        reg("run-1", "SSH_AUTH_SOCK", "/run/a.sock")
        reg("run-1", "SSH_AUTH_SOCK", None)
        assert relay.target is None

    def test_non_allowlisted_name_ignored(self):
        relay = _FakeRelay()
        reg = ChannelRelayRegistrar(relay)
        reg("run-1", "AWS_SECRET_KEY", "/run/evil.sock")
        assert relay.target is None
        assert relay.history == []

    def test_overlap_is_fail_closed_not_last_writer_wins(self):
        # Overlap policy (SECURITY): while run-1 owns the live target, a
        # publish from a DIFFERENT run must be REFUSED — the single fixed front
        # path carries no run identity, so silently re-pointing it would pump a
        # connection already blocked for run-1 to run-2's agent. run-2's git
        # fails closed and retries; run-1 keeps the target.
        relay = _FakeRelay()
        reg = ChannelRelayRegistrar(relay)
        reg("run-1", "SSH_AUTH_SOCK", "/run/a.sock")
        reg("run-2", "SSH_AUTH_SOCK", "/run/b.sock")  # overlap -> refused
        assert relay.target == "/run/a.sock"
        assert ("set", "/run/b.sock") not in relay.history
        # A late scrub from the NON-owning run-2 must not clear run-1's target.
        reg("run-2", "SSH_AUTH_SOCK", None)
        assert relay.target == "/run/a.sock"
        # The owning run releases the target on its own scrub...
        reg("run-1", "SSH_AUTH_SOCK", None)
        assert relay.target is None
        # ...after which a fresh run may take it.
        reg("run-3", "SSH_AUTH_SOCK", "/run/c.sock")
        assert relay.target == "/run/c.sock"

    def test_same_run_republish_is_idempotent(self):
        # A re-publish from the SAME owning run is allowed (idempotent), not
        # treated as a refused overlap.
        relay = _FakeRelay()
        reg = ChannelRelayRegistrar(relay)
        reg("run-1", "SSH_AUTH_SOCK", "/run/a.sock")
        reg("run-1", "SSH_AUTH_SOCK", "/run/a2.sock")
        assert relay.target == "/run/a2.sock"

    def test_owner_anchor_forwarded_to_relay(self):
        # The run's triggering pid + starttime must reach the relay so it can
        # bind connections to the owning run's process tree.
        relay = _FakeRelay()
        reg = ChannelRelayRegistrar(relay)
        reg("run-1", "SSH_AUTH_SOCK", "/run/a.sock",
            owner_pid=4321, owner_pid_starttime=99887766)
        assert relay.owner_pid == 4321


# ----------------------------------------------------------------------
# End-to-end through the WorkflowEngine
# ----------------------------------------------------------------------


class _AllowBroker:
    """Minimal broker stand-in; the engine only pokes WorkflowRunPending."""

    def WorkflowRunPending(self, *a, **k):
        return None


class _FakeSource:
    def __init__(self, key: bytes):
        self._key = key

    def fetch(self, item, run_id=""):
        return self._key


def _zero_coord_wf(runtime, hold_pid: int) -> WorkflowDef:
    """A zero-coord workflow: stand up the per-run agent (published to the
    relay), then ``wait_for_process`` on a live holder pid so the run stays
    RUNNING (agent live, relay pointed at it) until the test releases it."""
    return WorkflowDef(
        name="git-sign-zc",
        description="zero-coord",
        trigger=TriggerDef(type=TriggerType.CRON, config={"interval_seconds": 1}),
        needs=["vault/dev/github-ssh-key"],
        steps=[
            StepDef(
                type=StepType.DELIVER_SECRET,
                name="stand-up-agent",
                config={
                    "item": "vault/dev/github-ssh-key",
                    "as": "ssh-agent",
                    "expose_as": "SSH_AUTH_SOCK",
                    "ttl": 60,
                    "scrub_on": "workflow_exit",
                    "runtime_root": str(runtime),
                },
            ),
            StepDef(
                type=StepType.WAIT_FOR_PROCESS,
                name="hold",
                config={"pid": hold_pid, "timeout": 30},
            ),
        ],
    )


@pytest.mark.slow
@pytest.mark.needs_ssh
@pytest.mark.skipif(not _HAVE_SSH, reason="ssh tooling not available")
class TestEngineRelayEndToEnd:
    def test_published_socket_reaches_relay_then_scrubbed(self, tmp_path):
        key = _gen_key(tmp_path)
        runtime = tmp_path / "rt"
        front = str(tmp_path / "front.sock")

        relay, registrar = build_relay_registrar(front, connect_wait_s=8.0)
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        engine = WorkflowEngine(
            audit_logger=audit, broker_proxy=_AllowBroker(),
            secret_source=_FakeSource(key), channel_registrar=registrar)

        # A long-lived "holder" process keeps the run RUNNING (via
        # wait_for_process) so the per-run agent stays live while the
        # relayed client reads — mirrors a real git that has not yet
        # finished its commit.
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import time;time.sleep(30)"])
        engine._workflows["git-sign-zc"] = _zero_coord_wf(runtime, holder.pid)

        # The run executes on a background thread (start_run blocks in
        # wait_for_process until we kill the holder).
        run_box = {}

        def do_run():
            run_box["run"] = engine.start_run("git-sign-zc")

        # A client that connects to the FIXED front path BEFORE the run has
        # published — exactly the zero-coordination shape. It blocks in the
        # relay until set_target fires from deliver_secret.
        result = {}

        def client():
            result["proc"] = _ssh_add_list(front, timeout=10.0)

        try:
            _base = _blocked_conn_count()
            ct = threading.Thread(target=client, daemon=True)
            ct.start()
            # Readiness signal (replaces a fixed time.sleep(0.5)): wait until
            # the client is parked in the relay serve loop with no target yet
            # — the zero-coordination "connect before publish" shape — before
            # starting the run that will publish the per-run agent.
            _wait_for_blocked_conn(1, baseline=_base)
            rt = threading.Thread(target=do_run, daemon=True)
            rt.start()
            # The client should complete once deliver_secret publishes and
            # the registrar points the relay at the live per-run agent.
            ct.join(timeout=10.0)
            assert not ct.is_alive(), "relayed client never completed"
            assert result["proc"].returncode == 0, result["proc"].stderr
            assert "ED25519" in result["proc"].stdout
            # Now let the run finish: kill the holder so wait_for_process
            # returns and the run scrubs at workflow exit.
            holder.terminate()
            rt.join(timeout=15.0)
            run = run_box["run"]
            assert run.state == RunState.COMPLETED, run.error
            # The per-run socket was scrubbed at workflow exit...
            sock = run.context["channel_env"]["SSH_AUTH_SOCK"]
            assert not os.path.exists(sock)
            # ...and the registrar re-armed the relay to fail closed: a new
            # connection now gets no target and is closed at the deadline.
            assert _ssh_add_list(front, timeout=20.0).returncode != 0
            # The key never leaked into the run context.
            assert key.decode("latin1", "ignore")[:20] not in str(run.context)
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except Exception:
                holder.kill()
            relay.stop()


# ----------------------------------------------------------------------
# Engine scrub ordering + expose_as guard (no ssh needed)
# ----------------------------------------------------------------------


class _OrderRecorder:
    """Registrar + delivery handle that share one event log, so a test can
    assert the engine CLEARS the relay target BEFORE it scrubs the agent."""

    def __init__(self):
        self.events: list[str] = []

    # channel_registrar callable
    def __call__(self, run_id: str, name: str, value):
        self.events.append("publish" if value else "clear")

    def make_handle(self):
        rec = self

        class _Handle:
            method = "ssh-agent"
            auth_sock = "/run/qdistro/workflow-secrets/x/agent.sock"

            def scrub(self_inner):
                rec.events.append("scrub")

            def metadata(self_inner):
                return {"method": "ssh-agent", "scrubbed": True}

        return _Handle()


class TestEngineScrubOrdering:
    def test_target_cleared_before_agent_scrubbed(self):
        # Finding #3: a new relay connection in the window between killing the
        # agent and clearing the target would read a stale path. The engine
        # must clear() the registrar BEFORE scrub()-ing the delivery handle.
        rec = _OrderRecorder()
        engine = WorkflowEngine(channel_registrar=rec)
        # Build a RUNNING run with a tracked handle + a published channel.
        from workflow_schema import WorkflowRun  # noqa: E402
        r = WorkflowRun(workflow_name="zc")
        r.context["channel_env"] = {"SSH_AUTH_SOCK": rec.make_handle().auth_sock}
        engine._runs[r.run_id] = r
        engine._delivery_handles[r.run_id] = [rec.make_handle()]
        engine._delivered_secrets[r.run_id] = ["vault/dev/github-ssh-key"]

        engine._cleanup_secrets(r.run_id, "zc")

        assert "clear" in rec.events and "scrub" in rec.events
        assert rec.events.index("clear") < rec.events.index("scrub"), rec.events

    def test_non_allowlisted_expose_as_never_reaches_registrar(self):
        # Finding #4 / G: even if a workflow sets expose_as to a name outside
        # _EXTERNAL_CHANNEL_ENV_ALLOWLIST, _publish_channel must not hand it to
        # the registrar (which would otherwise be asked to point the relay at
        # an arbitrary path).
        seen = []
        engine = WorkflowEngine(
            channel_registrar=lambda rid, name, val: seen.append((name, val)))
        from workflow_schema import WorkflowRun, StepDef, StepType  # noqa: E402
        r = WorkflowRun(workflow_name="zc")

        class _Handle:
            method = "ssh-agent"
            auth_sock = "/run/qdistro/workflow-secrets/x/agent.sock"

        step = StepDef(type=StepType.DELIVER_SECRET, name="d",
                       config={"expose_as": "AWS_SECRET_ACCESS_KEY"})
        published = engine._publish_channel(_Handle(), step, r)

        # The name is still recorded on the run context (internal channel),
        # but it is NOT allowlisted, so the registrar is never notified.
        assert "AWS_SECRET_ACCESS_KEY" in published
        assert seen == []
