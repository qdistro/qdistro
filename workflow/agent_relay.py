"""Fixed-path ssh-agent relay for the zero-coordination git-signing flow.

This is the missing piece that makes a *pure* ``process_spawn(git)`` signing
flow work without the call site knowing anything about qdistro.

Background — why the other two flows can't do this
--------------------------------------------------
The inline flow (git-sign-inline.yaml) works because the ENGINE launches the
signing command and can fold ``SSH_AUTH_SOCK`` into the child's env at
``exec`` time. The external-bridge flow (git-sign-commit.yaml + ``qsu
--workflow-run``) works because ``qsu``/``qdistro-root-exec`` launch git and
fold the env in at exec time. Both inject the socket *before* git execs.

A dev who just runs ``git commit -S`` with no wrapper gives us neither hook:

  1. **Env is frozen at exec.** You cannot push ``SSH_AUTH_SOCK`` into a git
     process that is already running — the environment is fixed the instant
     ``execve`` returns. So the value git's ssh will use must already be in
     git's environment when it starts.
  2. **Publish happens too late.** The ``process_spawn`` trigger only fires
     AFTER the broker's watcher observes git; the run is then PENDING until an
     admin approves it and the ``deliver_secret`` step finally stands up the
     per-run agent and publishes its socket. By then git has been running for
     a while and — left to itself — would have already failed to sign.

The relay
---------
We make the dev's environment point ``SSH_AUTH_SOCK`` (or, equivalently,
``~/.ssh/config``'s ``IdentityAgent``) at ONE fixed, well-known per-user path
ahead of time (a one-time profile edit; see git-sign-zero-coord.yaml). The
relay binds that path and is always listening, so:

  - git's ssh ``connect()`` to the fixed path ALWAYS succeeds immediately —
    there is no "socket does not exist yet" race, because the path is bound
    by the long-lived relay, not by the per-run agent.
  - The relay then holds the accepted connection and waits, up to a bounded
    deadline, for the engine to register THIS run's freshly-published per-run
    agent socket (``set_target``). git's ssh blocks harmlessly on the agent
    protocol read while that window plays out.
  - Once a target is registered, the relay dials the real per-run agent and
    transparently byte-pumps both directions. The ssh-agent wire protocol is
    an opaque request/response stream over a stream socket, so a blind
    bidirectional copy is correct and version-agnostic — we never parse or
    interpret key material.
  - On deadline with no target, the relay closes the connection. ssh sees an
    agent failure and git fails to sign — FAIL CLOSED. The relay never points
    at a stale agent (the engine clears the target before tearing the agent
    down) and never serves another run's git: each connection's peer is
    checked, via unforgeable SO_PEERCRED, to be in the owning run's process
    tree before any byte flows (see _serve). No key material is ever exposed by
    the relay itself (it holds none — only a path to the real agent).

Security posture
----------------
  - The relay socket is 0600 inside a 0700 per-user directory, so only the
    owning uid can connect (first boundary). On top of that, each connection
    is bound to the OWNING RUN: the relay reads the peer's pid via SO_PEERCRED
    (kernel-attested, unforgeable) and relays only if that peer is the run's
    triggering git or a descendant of it (pid-reuse anchored on the captured
    /proc starttime). So even within one uid, a process that is not part of
    the approved run's git tree cannot use a live target. Caveat: the ancestry
    walk trusts /proc; on a shared account an attacker who can interpose a
    parent in the target git's own process tree is out of scope (as it is for
    any /proc-based check). The relay carries NO secret — it only forwards
    bytes to a per-run agent whose key has a TTL and is scrubbed by the engine
    on workflow exit.
  - ``set_target`` only ever accepts a path under the engine's per-run secret
    root (it is the engine, post-publish, that calls it). ``clear_target``
    re-arms the relay to fail closed as part of run scrub; the engine clears
    the target BEFORE it kills/removes the per-run agent, so a connection
    arriving during teardown does not even see the dead target path.
  - The relay is a CONVENIENCE that removes the call-site coordination; it
    does NOT remove the admin-approval gate. A connection that arrives while
    the run is still pending simply blocks (and fails closed if approval does
    not land within the relay's window). The key is never released without
    the normal deliver_secret approval choreography having published it.

Residual race (documented, not papered over)
--------------------------------------------
git's own ssh has a finite patience for the agent socket. If admin approval
takes longer than that client-side timeout (or longer than ``connect_wait``),
git's signing attempt fails closed and the dev must retry. This relay turns
the *unsolvable* "inject into a running process" problem into a *bounded
liveness* problem (does approval+publish beat the client timeout?), which is
the soundest a true zero-coordination flow can be. It does not, and cannot,
make a human approval instantaneous.
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time

logger = logging.getLogger("qdistro.workflow.agent_relay")

# How long an accepted front connection waits for a per-run target to be
# registered before giving up and failing closed. Sized to cover the publish
# lag of an ALREADY-APPROVED run; it deliberately does NOT try to outlast a
# human approval (git's own ssh would time out first anyway).
_DEFAULT_CONNECT_WAIT_S = 8.0
_TARGET_POLL_S = 0.05

# Copy buffer for the byte pump. The ssh-agent protocol frames are small
# (a signature request/response is a few KiB), so 64 KiB is ample.
_PUMP_BUF = 65536

# Bound the /proc ppid walk so a pathological (or hostile) ancestry chain
# can't spin the relay thread. A real git -> ssh-keygen/ssh tree is 1-3 deep.
_MAX_ANCESTRY_DEPTH = 64


def _peer_pid(conn: socket.socket) -> int | None:
    """Return the connecting peer's pid via SO_PEERCRED (kernel-attested).

    The kernel stamps the pid at connect() time; it cannot be forged by the
    peer. None if it can't be read (non-AF_UNIX, or the peer already gone).
    """
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", creds)
        return pid or None
    except OSError:
        return None


def _proc_stat_tail(pid: int) -> list[bytes] | None:
    """Fields of /proc/<pid>/stat AFTER comm, or None if unreadable.

    comm (field 2) is parenthesised and may itself contain ')'/spaces, so we
    split on the LAST ')': everything after it has stable field offsets. In the
    returned tail, index 0 = state (field 3), index 1 = ppid (field 4),
    index 19 = starttime (field 22).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    return data[rparen + 2:].split()


def _proc_ppid(pid: int) -> int | None:
    tail = _proc_stat_tail(pid)
    try:
        return int(tail[1]) if tail else None
    except (ValueError, IndexError):
        return None


def _proc_starttime(pid: int) -> int | None:
    tail = _proc_stat_tail(pid)
    try:
        return int(tail[19]) if tail else None
    except (ValueError, IndexError):
        return None


def _is_self_or_descendant(peer_pid: int, owner_pid: int,
                           owner_start: int | None) -> bool:
    """True iff ``peer_pid`` is ``owner_pid`` or one of its descendants.

    This is the relay's run-binding: a connection is relayed to a run's agent
    ONLY when the connecting process belongs to that run's triggering process
    tree (the dev's git and the ssh/ssh-keygen children it spawns to talk to
    the agent). Walks the /proc ppid chain up from the peer.

    Pid-reuse anchored, FAIL-CLOSED on a missing anchor: ``owner_start`` is the
    triggering git's /proc starttime. If it is None (the trigger could not
    capture it) we REFUSE — without the anchor we cannot tell the original git
    from a process that later recycled its pid, so binding would be unsound.
    If it is present but the live owner pid's starttime no longer matches, the
    original git has exited and the pid was recycled — refuse (stale binding).
    Fail-closed on any unreadable /proc entry (returns False).

    Known residual (documented): the anchor covers the OWNER pid, but the walk
    reads live ppids of the peer and intermediate ancestors. A local same-uid
    attacker who can win a pid-reuse race — exiting an intermediate pid and
    having a process in the owner tree immediately reclaim it between two
    /proc reads — could in theory mis-link an outside peer. This is inherent to
    any /proc ancestry check and is far narrower than the overlap blocker this
    gate closes; a fully race-free check would need a pidfd/cgroup-scoped API.
    """
    # No anchor -> cannot trust the pid -> fail closed (do NOT degrade to an
    # un-anchored walk). The engine supplies the anchor for process_spawn runs.
    if owner_start is None:
        return False
    live = _proc_starttime(owner_pid)
    if live is None or live != owner_start:
        return False
    pid = peer_pid
    depth = 0
    while pid and pid > 1 and depth <= _MAX_ANCESTRY_DEPTH:
        if pid == owner_pid:
            return True
        nxt = _proc_ppid(pid)
        if nxt is None or nxt == pid:
            return False
        pid = nxt
        depth += 1
    return pid == owner_pid


class SshAgentRelay:
    """A fixed-path Unix socket that relays to a swappable per-run agent.

    Lifecycle:
        relay = SshAgentRelay(front_path)
        relay.start()                 # binds + listens; connects now block
        relay.set_target(agent_sock)  # engine, after deliver publishes
        ...
        relay.clear_target()          # engine, on scrub — re-arm fail-closed
        relay.stop()                  # teardown

    Thread-safe: ``set_target``/``clear_target`` may be called from the
    engine's run thread while connections are being served.
    """

    def __init__(self, front_path: str, *,
                 connect_wait_s: float = _DEFAULT_CONNECT_WAIT_S):
        self._front_path = front_path
        self._connect_wait_s = max(0.0, float(connect_wait_s))
        self._target: str | None = None
        # Owner anchor for run-binding: the triggering git's pid + its /proc
        # starttime (pid-reuse anchor). When set, a front connection is relayed
        # ONLY if its peer belongs to this process tree (see _serve). None ->
        # uid-only (legacy/direct set_target; the engine always supplies it).
        self._owner_pid: int | None = None
        self._owner_start: int | None = None
        self._target_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def front_path(self) -> str:
        return self._front_path

    # -- target registration -------------------------------------------------

    def set_target(self, agent_sock: str, *, owner_pid: int | None = None,
                   owner_pid_starttime: int | None = None) -> None:
        """Point the relay at the live per-run agent socket.

        Called by the engine right after a ``deliver_secret`` step publishes
        ``SSH_AUTH_SOCK``. Connections currently blocked in ``_serve`` waiting
        for a target pick this up on their next poll.

        ``owner_pid`` (+ its ``owner_pid_starttime`` pid-reuse anchor) is the
        run's triggering git pid. When given, ``_serve`` relays ONLY to a peer
        in that process tree — so a *second* dev's git, even at the same uid,
        cannot ride a first run's live target (it fails closed). When None the
        relay falls back to uid-only (direct/test callers); the engine always
        supplies it, so the real zero-coordination flow is always run-bound.
        """
        with self._target_lock:
            self._target = agent_sock
            self._owner_pid = owner_pid
            self._owner_start = owner_pid_starttime

    def clear_target(self) -> None:
        """Re-arm fail-closed: drop the target so new (and waiting)
        connections no longer relay. Called on scrub so a request arriving
        after the agent is torn down cannot reach a dead socket."""
        with self._target_lock:
            self._target = None
            self._owner_pid = None
            self._owner_start = None

    def _current_target(self) -> tuple[str | None, int | None, int | None]:
        with self._target_lock:
            return self._target, self._owner_pid, self._owner_start

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Bind the fixed front path 0600 and start accepting.

        The parent directory is created 0700 so only the owning uid can even
        reach the socket. We unlink any stale socket first (a crashed prior
        relay) — the 0700 parent means no other local uid could have planted
        one there.
        """
        parent = os.path.dirname(self._front_path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            # Tighten in case it pre-existed with looser perms.
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        try:
            os.unlink(self._front_path)
        except FileNotFoundError:
            pass
        lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        lst.bind(self._front_path)
        os.chmod(self._front_path, 0o600)
        lst.listen(16)
        lst.settimeout(0.5)  # so the accept loop can observe _stop
        self._listener = lst
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="ssh-agent-relay", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        lst = self._listener
        self._listener = None
        if lst is not None:
            try:
                lst.close()
            except OSError:
                pass
        # Join the accept loop briefly: it polls _stop every 0.5s, so it
        # winds down promptly; bounded so stop() never hangs on a wedged loop.
        t = self._accept_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        try:
            os.unlink(self._front_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # -- accept + serve ------------------------------------------------------

    def _accept_loop(self) -> None:
        lst = self._listener
        if lst is None:
            return
        while not self._stop.is_set():
            try:
                conn, _ = lst.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,),
                             name="ssh-agent-relay-conn", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        """Wait (bounded) for a target, verify the peer belongs to the owning
        run, then transparently pump bytes.

        Two fail-closed gates run before a single byte is relayed:

          1. A target must be registered within ``connect_wait_s`` (else the
             connection is closed — ssh reads a dead agent and git fails to
             sign rather than signing against some other agent).
          2. When the target carries an owner anchor (the run's triggering git
             pid, supplied by the engine), the connecting peer — attested by
             SO_PEERCRED, which the peer cannot forge — must BE that pid or one
             of its descendants (the ssh/ssh-keygen process git spawns to reach
             the agent). A *second* dev's git, even at the same uid, is not in
             that process tree, so it is refused even while a target is live.
             This is what makes overlap genuinely fail-closed for the second
             run, not merely "the target is not moved".
        """
        upstream: socket.socket | None = None
        try:
            deadline = time.monotonic() + self._connect_wait_s
            target, owner_pid, owner_start = self._current_target()
            while target is None and time.monotonic() < deadline:
                if self._stop.is_set():
                    return
                time.sleep(_TARGET_POLL_S)
                target, owner_pid, owner_start = self._current_target()
            if target is None:
                logger.info("agent-relay: no target within %.0fs; failing "
                            "closed for a connection on %s",
                            self._connect_wait_s, self._front_path)
                return
            # Run-binding gate (fail-closed). owner_pid is None only for
            # direct/uid-only callers (e.g. transport tests); the engine always
            # supplies it for the real zero-coordination flow.
            if owner_pid is not None:
                peer = _peer_pid(conn)
                if peer is None or not _is_self_or_descendant(
                        peer, owner_pid, owner_start):
                    logger.warning(
                        "agent-relay: peer pid %r not in owning run pid %s's "
                        "process tree; failing closed", peer, owner_pid)
                    return
            try:
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                upstream.connect(target)
            except OSError as e:
                logger.warning("agent-relay: cannot reach target %s: %r",
                               target, e)
                return
            self._pump_bidir(conn, upstream)
        except Exception as e:  # noqa: BLE001 — never let one conn kill the relay
            logger.warning("agent-relay serve error: %r", e)
        finally:
            for s in (conn, upstream):
                if s is None:
                    continue
                try:
                    s.close()
                except OSError:
                    pass

    @staticmethod
    def _pump_bidir(a: socket.socket, b: socket.socket) -> None:
        """Copy bytes a<->b until either side closes.

        The ssh-agent protocol is opaque request/response framing; we never
        parse it, so this blind copy is correct for every agent version.
        """
        def half(src: socket.socket, dst: socket.socket) -> None:
            try:
                while True:
                    data = src.recv(_PUMP_BUF)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                # Half-close so the peer sees EOF and the other half ends.
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        t1 = threading.Thread(target=half, args=(a, b), daemon=True)
        t2 = threading.Thread(target=half, args=(b, a), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()


# ----------------------------------------------------------------------
# Engine glue: run-aware registrar
# ----------------------------------------------------------------------

# Only this channel name is ever relayed. Same value as the engine's
# _EXTERNAL_CHANNEL_ENV_ALLOWLIST — duplicated here (not imported) so the
# relay is a second, independent fail-closed gate: even if the engine's
# allowlist ever widened, the relay still only points at an SSH_AUTH_SOCK.
_RELAYABLE_CHANNELS = frozenset({"SSH_AUTH_SOCK"})


class ChannelRelayRegistrar:
    """Adapt the engine's ``(run_id, name, value|None)`` channel callback to
    an :class:`SshAgentRelay`.

    The engine calls this:
      - ``(run_id, "SSH_AUTH_SOCK", "/run/.../agent.sock")`` when a run
        publishes its per-run agent socket, and
      - ``(run_id, "SSH_AUTH_SOCK", None)`` when that run is scrubbed.

    Run-ownership: the relay has exactly ONE live target (one fixed per-user
    front path can only point at one agent at a time). We record which run
    currently owns the target so a SCRUB from an *older* run cannot clear a
    *newer* run's target — only the owning run's clear re-arms fail-closed.
    Overlap is FAIL-CLOSED, not last-writer-wins: the single fixed front path
    has exactly one live target and carries no run identity, so a connection
    already blocked in the relay cannot be told apart by run. While one run
    owns the target, a publish from a *different* run is refused (its git
    fails closed and the dev retries); only the owning run's scrub releases
    the target. This serializes overlapping signing runs rather than risk
    relaying one run's git to another run's agent. Non-relayable names are
    ignored — defense-in-depth on top of the engine's own allowlist.

    Thread-safe: the engine may publish/scrub from different worker threads.
    """

    def __init__(self, relay: SshAgentRelay):
        self._relay = relay
        self._lock = threading.Lock()
        self._owner_run: str | None = None

    def __call__(self, run_id: str, name: str, value: str | None,
                 owner_pid: int | None = None,
                 owner_pid_starttime: int | None = None) -> None:
        if name not in _RELAYABLE_CHANNELS:
            return
        with self._lock:
            if value:
                # Publish: take ownership and point the relay at this agent.
                # Fail closed on OVERLAP: if a *different* run already owns the
                # live target, refuse to move it (two layers of overlap
                # safety). This registrar layer keeps the FIRST run's target
                # in place; the relay's per-connection SO_PEERCRED ancestry
                # gate (see _serve) is what makes the SECOND run's git actually
                # fail closed — it is not in the first run's process tree, so
                # even though the live target stays, its connection is refused.
                # A re-publish from the SAME owning run is idempotent.
                if self._owner_run is not None and self._owner_run != run_id:
                    logger.warning(
                        "agent-relay: refusing to move target to run %s; run "
                        "%s still owns it (overlapping signing runs are "
                        "serialized, fail-closed)", run_id, self._owner_run)
                    return
                self._owner_run = run_id
                self._relay.set_target(
                    value, owner_pid=owner_pid,
                    owner_pid_starttime=owner_pid_starttime)
            else:
                # Scrub: only the owning run may re-arm fail-closed. A stale
                # clear from a superseded run is ignored.
                if self._owner_run == run_id:
                    self._owner_run = None
                    self._relay.clear_target()


def build_relay_registrar(front_path: str, *,
                          connect_wait_s: float = _DEFAULT_CONNECT_WAIT_S
                          ) -> tuple[SshAgentRelay, ChannelRelayRegistrar]:
    """Construct + start an :class:`SshAgentRelay` on ``front_path`` and
    return ``(relay, registrar)``. Pass the registrar as the engine's
    ``channel_registrar``; keep a handle on the relay so the broker can
    ``stop()`` it on shutdown."""
    relay = SshAgentRelay(front_path, connect_wait_s=connect_wait_s)
    relay.start()
    return relay, ChannelRelayRegistrar(relay)
