"""Core workflow engine.

Orchestrates workflow execution: loads definitions, registers
triggers, executes steps in order, audits each step, and handles
failure cleanup (secret scrubbing, run-state marking).
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import select
import threading
import time
from typing import Any

from workflow_schema import (  # type: ignore[import-not-found]
    RunState,
    StepDef,
    StepResult,
    StepType,
    WorkflowDef,
    WorkflowRun,
)
from workflow_loader import WorkflowLoader  # type: ignore[import-not-found]
from trigger_registry import TriggerRegistry  # type: ignore[import-not-found]
from audit_logger import WorkflowAuditLogger  # type: ignore[import-not-found]
import condition_eval  # type: ignore[import-not-found]
from secret_delivery import (  # type: ignore[import-not-found]
    DeliveryError,
    DeliveryHandle,
    SecretValue,
    make_delivery,
    reap_runtime_root,
)

logger = logging.getLogger("qdistro.workflow.engine")

# scrub_on values whose secret lifetime ends with the deliver step
# itself (the channel is revoked immediately rather than at run exit).
_STEP_SCRUB_LIFETIMES = frozenset({"step", "step_exit", "immediate", "now"})

# Allowlist of channel_env variable names that may be handed to an
# EXTERNALLY launched process via the qsu/root-exec bridge
# (GetRunChannelEnv). These are non-secret *references* (a unix-socket
# path) published by _publish_channel — never secret material. The
# allowlist is the load-bearing fail-closed gate: a run may publish other
# keys into channel_env (now or in future), but only names in this set
# are ever exposed to an external child's environment. Adding a name here
# is a deliberate, reviewable act — never widen it to a wildcard.
_EXTERNAL_CHANNEL_ENV_ALLOWLIST = frozenset({"SSH_AUTH_SOCK"})

# Broker methods a ``call_broker`` step is allowed to invoke. Restricted
# to read-only / maintenance surface so a workflow can never drive the
# broker's decision or secret machinery (RequestPermission, DecideRequest,
# SaveRule, etc. are deliberately excluded — fail closed on anything else).
_BROKER_METHOD_WHITELIST = frozenset({
    "ListRules",
    "ListCache",
    "ListHistory",
    "ListWorkflows",
    "ListWorkflowRuns",
    "GetPending",
    "RunCacheGc",
    "RunAuditGc",
    "ReloadRules",
})

# call_dbus destination policy (F2). call_dbus can reach any system-bus
# service *as root* (the engine runs inside the root broker), so — unlike
# call_broker's allowlist — it must NOT be an unrestricted escape hatch.
# Two layers: a hard denylist of bus names a workflow may never call
# (the secret/vault daemon, init/login managers — privilege-escalation
# surfaces), and an optional allowlist. When the allowlist is non-empty
# (configured per deployment via QDISTRO_WORKFLOW_DBUS_ALLOW, comma-sep),
# ONLY those bus names are permitted (default-deny); when empty, any
# non-denied bus name is permitted (default-allow minus the denylist).
_DBUS_BUS_NAME_DENYLIST = frozenset({
    "org.qdistro.Pwd1",            # vault/secret daemon
    "org.qdistro.AdminBroker1",    # our own broker (use call_broker, whitelisted)
    "org.freedesktop.systemd1",    # init manager — unit start/stop as root
    "org.freedesktop.login1",      # session/seat/power manager
    "org.freedesktop.PolicyKit1",  # authorization authority
})


def _dbus_allowlist() -> frozenset[str]:
    raw = os.environ.get("QDISTRO_WORKFLOW_DBUS_ALLOW", "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _dbus_hardening_enabled() -> bool:
    """Whether the call_dbus destination policy (denylist + unique-name
    rejection + allowlist) is active.

    DEFAULT OFF — call_dbus is wide open for now to keep workflow testing
    unobstructed; hardening is a deliberate later step (see
    todo/issues/qdistro/qdistro-workflow-dbus-hardening.md). Flip on with
    QDISTRO_WORKFLOW_DBUS_HARDENING=1. The code below stays in place so
    enabling is a one-flag change once testing settles."""
    return os.environ.get(
        "QDISTRO_WORKFLOW_DBUS_HARDENING", "").strip().lower() in (
        "1", "true", "yes", "on")


# Default + ceiling for step-level bounded waits/calls (seconds). A step
# config may lower these but never raise above the ceiling, so a typo in
# YAML can't pin a worker thread forever.
_DEFAULT_WAIT_TIMEOUT_S = 300.0
_MAX_WAIT_TIMEOUT_S = 3600.0
_DEFAULT_DBUS_TIMEOUT_S = 10.0
_MAX_DBUS_TIMEOUT_S = 120.0


def _clamp_timeout(raw: Any, default: float, ceiling: float) -> float:
    """Coerce a config timeout to a positive float within [.., ceiling]."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val <= 0:
        return default
    return min(val, ceiling)


def _proc_starttime(pid: int) -> int | None:
    """Read /proc/<pid>/stat field 22 (starttime, clock ticks since boot).

    Used as an anti-PID-reuse anchor. Returns None if unreadable (the
    process is gone or /proc is unavailable).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        # comm (field 2) is parenthesised and may contain spaces/')':
        # split on the LAST ')' so field offsets after it are stable.
        rparen = data.rfind(b")")
        fields = data[rparen + 2:].split()
        # After comm, fields[0] is state (field 3); starttime is field 22,
        # i.e. index 22 - 3 = 19 in this tail slice.
        return int(fields[19])
    except (ValueError, IndexError):
        return None


def _return_shape(value: Any) -> dict[str, Any]:
    """Describe a call's return *without* copying its payload.

    Step details are persisted to the audit DB and a broker/D-Bus method
    can return secret text or byte data, so we record only the type and,
    where meaningful, a length — never the value itself.
    """
    shape: dict[str, Any] = {"returned_type": type(value).__name__}
    try:
        if value is None:
            shape["returned"] = False
        else:
            shape["returned"] = True
            if isinstance(value, (str, bytes, bytearray, list, tuple, dict)):
                shape["returned_len"] = len(value)
    except Exception:  # noqa: BLE001
        pass
    return shape


def _resolve_ref(value: Any, run: WorkflowRun) -> Any:
    """Resolve a ``$trigger.<key>`` / ``$run.<key>`` reference.

    ``$trigger.*`` reads the firing trigger's context; ``$run.*`` reads
    the run's published channel context (a deliver_secret step may publish
    e.g. ``SSH_AUTH_SOCK`` for a later step to reference). A plain value is
    returned as-is.
    """
    if isinstance(value, str):
        for prefix in ("$trigger.", "trigger."):
            if value.startswith(prefix):
                return run.trigger_context.get(value[len(prefix):])
        for prefix in ("$run.", "run."):
            if value.startswith(prefix):
                key = value[len(prefix):]
                chan = run.context.get("channel_env", {})
                if key in chan:
                    return chan[key]
                return run.context.get(key)
    return value


class WorkflowEngine:
    """Loads workflow definitions, registers triggers, executes runs.

    Parameters
    ----------
    broker_proxy:
        Optional D-Bus proxy to the admin broker. Used by step
        handlers that need to call broker methods (e.g. call_broker,
        deliver_secret). Pass None for standalone / test usage.
    audit_logger:
        WorkflowAuditLogger instance for recording run audit trails.
    loader:
        WorkflowLoader instance. If None, a default loader is created.
    secret_source:
        Callable ``fetch(item: str) -> bytes`` that unseals a vault item
        for delivery (e.g. the pwd daemon's DeliverToWorkflow client).
        When None, ``deliver_secret`` runs in tracking-only mode (no real
        secret is fetched or delivered) so the engine can be exercised
        without a vault backend.
    """

    def __init__(
        self,
        broker_proxy: Any | None = None,
        audit_logger: WorkflowAuditLogger | None = None,
        loader: WorkflowLoader | None = None,
        secret_source: Any | None = None,
        own_dbus_loop: bool = True,
        max_concurrent_runs: int = 4,
    ):
        self._broker = broker_proxy
        self._audit = audit_logger
        self._loader = loader or WorkflowLoader()
        self._registry = TriggerRegistry(dbus_run_own_loop=own_dbus_loop)
        self._secret_source = secret_source
        # Runs execute on a worker pool, never on the firing thread, so a
        # trigger fire (especially a D-Bus signal delivered on the broker's
        # shared GLib main loop) returns immediately and a long-running
        # workflow can't block the loop, other watchers, or reload.
        self._run_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(max_concurrent_runs)),
            thread_name_prefix="workflow-run")
        # F8: bound queued+running work so a burst of trigger fires (e.g.
        # a process tree churning pids) can't grow the executor's
        # unbounded queue without limit. Excess fires are dropped with a
        # log line rather than silently piling up.
        self._max_inflight = max(1, int(max_concurrent_runs)) + 64
        self._inflight = 0
        # F6: collapse concurrent auto-run fires for the same
        # (workflow, cgroup) so one process tree doesn't launch many
        # parallel secret deliveries. Also guards _inflight.
        self._active_keys: set[tuple[str, str]] = set()
        self._active_lock = threading.Lock()
        self._workflows: dict[str, WorkflowDef] = {}
        # F9: definitions whose triggers are currently live, so a reload
        # can skip re-registering unchanged workflows.
        self._registered_defs: dict[str, WorkflowDef] = {}
        self._runs: dict[str, WorkflowRun] = {}
        self._runs_lock = threading.Lock()
        # Secret item names delivered during runs, keyed by run_id.
        # Kept for audit/scrub bookkeeping (never holds secret values).
        self._delivered_secrets: dict[str, list[str]] = {}
        # Live delivery handles per run_id; scrub() actually revokes.
        self._delivery_handles: dict[str, list[DeliveryHandle]] = {}
        # Guards the two dicts above: runs execute on the worker pool, so
        # delivery (worker thread) races cleanup/scrub_all (shutdown
        # thread). Set during shutdown so a late delivery scrubs at once
        # instead of being tracked into a dict that's already drained.
        self._secrets_lock = threading.Lock()
        self._stopping = False
        # When a real vault backend is wired up, sweep any secret dirs a
        # previously-crashed engine left mounted/on tmpfs before we start.
        if secret_source is not None:
            try:
                reaped = reap_runtime_root()
                if reaped:
                    logger.warning(
                        "reaped %d stale secret dir(s) from a prior run",
                        reaped)
            except Exception as e:  # noqa: BLE001
                logger.warning("startup secret reap failed: %r", e)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load_workflows(self) -> list[str]:
        """Load workflow definitions from disk. Returns load errors."""
        wf_list = self._loader.load()
        self._workflows = {wf.name: wf for wf in wf_list}
        errors = self._loader.load_errors()
        for err in errors:
            logger.warning("workflow load error: %s", err)
        logger.info("loaded %d workflow(s)", len(self._workflows))
        return errors

    def register_triggers(self) -> None:
        """Register triggers for all loaded workflows.

        Also unregisters triggers for workflows that are no longer
        loaded (e.g. after a reload removed a YAML file).

        F9: only (re-)register workflows whose definition actually changed
        since the last call. An unchanged workflow keeps its live trigger,
        so a reload triggered by an unrelated rules-dir edit does not tear
        down and re-baseline every ProcessSpawnTrigger (which would open a
        window where freshly-spawned processes are missed).
        """
        # Unregister triggers for workflows that were removed.
        current_names = set(self._workflows.keys())
        stale_names = set(self._registry.active_triggers().keys()) - current_names
        for name in stale_names:
            self._registry.unregister(name)
            self._registered_defs.pop(name, None)
            logger.info("unregistered stale trigger for removed workflow %s",
                        name)

        for wf in self._workflows.values():
            prev = self._registered_defs.get(wf.name)
            if prev is not None and prev == wf:
                # Definition unchanged — leave the existing trigger alone.
                continue
            try:
                self._registry.register(wf, self._on_trigger)
                self._registered_defs[wf.name] = wf
                logger.info(
                    "registered trigger for workflow %s (type=%s)",
                    wf.name, wf.trigger.type.value,
                )
            except Exception as e:  # noqa: BLE001
                self._registered_defs.pop(wf.name, None)
                logger.error(
                    "failed to register trigger for workflow %s: %s",
                    wf.name, e,
                )

    def shutdown(self) -> None:
        """Stop all triggers, drain runs, scrub outstanding secrets."""
        self._registry.unregister_all()
        # Flip the stopping flag first so any delivery that lands after the
        # drain scrubs itself immediately instead of being tracked into a
        # dict scrub_all_runs has already emptied.
        with self._secrets_lock:
            self._stopping = True
        # Cancel queued runs; wait for in-flight ones to finish so they
        # self-scrub at their own run exit before we sweep stragglers.
        try:
            self._run_pool.shutdown(wait=True, cancel_futures=True)
        except TypeError:  # pragma: no cover - py<3.9
            self._run_pool.shutdown(wait=True)
        self.scrub_all_runs()
        logger.info("workflow engine shut down")

    # ------------------------------------------------------------------
    # Run management
    # ------------------------------------------------------------------

    def start_run(
        self,
        workflow_name: str,
        trigger_context: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        """Create and execute a workflow run.

        Iterates through steps sequentially. Each step is audited.
        On failure mid-step, delivered secrets are scrubbed and the
        run is marked failed.
        """
        wf = self._workflows.get(workflow_name)
        if wf is None:
            raise ValueError(f"unknown workflow: {workflow_name!r}")

        run = WorkflowRun(
            workflow_name=workflow_name,
            trigger_context=dict(trigger_context or {}),
        )
        run.mark_running()

        with self._runs_lock:
            self._runs[run.run_id] = run

        return self._execute_existing_run(run)

    def _execute_existing_run(self, run: WorkflowRun) -> WorkflowRun:
        """Execute a run that has already been created + stored.

        Shared by ``start_run`` (immediate execution) and ``approve_run``
        (a previously-PENDING run). Logs the run start, applies the
        fail-closed conditions gate, iterates steps, and finalises
        (scrub + audit) on success or failure.
        """
        workflow_name = run.workflow_name
        wf = self._workflows.get(workflow_name)
        if wf is None:
            error_msg = "workflow no longer loaded"
            run.mark_failed(error_msg)
            if self._audit:
                self._audit.log_run_failed(
                    run.run_id, workflow_name, error_msg)
            return run

        if run.state != RunState.RUNNING:
            run.mark_running()

        if self._audit:
            self._audit.log_run_start(
                run.run_id, workflow_name, run.trigger_context,
            )

        logger.info(
            "starting workflow run %s for %s",
            run.run_id, workflow_name,
        )

        # Fail-closed identity gate: re-evaluate conditions against the
        # live firing process at execution time (also defeats TOCTOU vs.
        # an approval delay). No secret is fetched if this rejects.
        ok, reason = self._conditions_satisfied(wf, run)
        if not ok:
            error_msg = f"conditions not satisfied: {reason}"
            logger.warning("workflow %s run %s rejected: %s",
                           workflow_name, run.run_id, error_msg)
            run.mark_failed(error_msg)
            if self._audit:
                self._audit.log_run_failed(
                    run.run_id, workflow_name, error_msg)
            return run

        try:
            self._execute_steps(wf, run)
        except Exception as e:  # noqa: BLE001
            error_msg = f"uncaught exception: {e!r}"
            logger.error("workflow %s run %s failed: %s",
                         workflow_name, run.run_id, error_msg)
            run.mark_failed(error_msg)
            self._cleanup_secrets(run.run_id, workflow_name)
            if self._audit:
                self._audit.log_run_failed(
                    run.run_id, workflow_name, error_msg,
                )
            return run

        if run.state == RunState.RUNNING:
            run.mark_completed()
            # Scrub secrets delivered with scrub_on="workflow_exit"
            # (the default). A real backend would revoke the secret;
            # the skeleton just removes the tracking entry.
            self._cleanup_secrets(run.run_id, workflow_name)
            if self._audit:
                self._audit.log_run_complete(run.run_id, workflow_name)
            logger.info("workflow %s run %s completed",
                        workflow_name, run.run_id)

        return run

    def get_run(self, run_id: str) -> WorkflowRun | None:
        with self._runs_lock:
            return self._runs.get(run_id)

    def list_runs(self) -> list[WorkflowRun]:
        with self._runs_lock:
            return list(self._runs.values())

    def list_workflow_defs(self) -> list[dict[str, Any]]:
        """Secret-free metadata for each loaded workflow (for the admin
        D-Bus list surface)."""
        out: list[dict[str, Any]] = []
        for wf in self._workflows.values():
            out.append({
                "name": wf.name,
                "trigger_type": wf.trigger.type.value,
                "description": wf.description,
                "needs": list(wf.needs),
                "step_count": len(wf.steps),
                "source_path": wf.source_path,
            })
        return out

    def recent_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent run records from the audit DB (durable across restarts).
        Falls back to in-memory runs when no audit logger is configured."""
        if self._audit is not None:
            return self._audit.recent_runs(limit)
        runs = sorted(self.list_runs(), key=lambda r: r.started_at,
                      reverse=True)[:limit]
        return [
            {
                "run_id": r.run_id, "workflow_name": r.workflow_name,
                "state": r.state.value, "started_at": r.started_at,
                "completed_at": r.completed_at, "error": r.error,
            }
            for r in runs
        ]

    # ------------------------------------------------------------------
    # External-process channel bridge (qsu / root-exec)
    # ------------------------------------------------------------------

    def _run_trigger_uid(self, run: WorkflowRun) -> int | None:
        """Best-effort uid of the run's TRIGGERING process.

        Used to bind the external channel-env bridge to the caller: only a
        process owned by the same uid as the run's trigger may inherit the
        run's socket. Derived once from ``trigger_context['pid']`` (the
        process_spawn watcher's pid), PID-reuse-anchored against the
        captured ``pid_starttime``, and cached on the run. Returns None when
        the run has no triggering pid (cron / engine-launched runs — those
        are NOT reachable by the uid-bound external bridge), the pid has
        exited, or the pid was recycled (anchor mismatch).
        """
        cached = run.context.get("_trigger_uid")
        if cached is not None:
            return int(cached) if cached >= 0 else None
        pid = run.trigger_context.get("pid")
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            run.context["_trigger_uid"] = -1
            return None
        ident = condition_eval._read_identity(pid_i)  # type: ignore[attr-defined]
        uid = None
        if ident is not None:
            # PID-reuse anchor: if the live pid's starttime differs from the
            # one captured at fire, the original triggering process is gone
            # and this pid is a stranger — refuse to bind to it.
            expected = run.trigger_context.get("pid_starttime")
            if expected is not None:
                now_start = _proc_starttime(pid_i)
                if now_start is not None and now_start != expected:
                    ident = None
            if ident is not None:
                uid = ident.get("uid")
        # Cache (-1 sentinel = "no bindable uid") so a later exit/recycle
        # doesn't flip the answer mid-run after we first resolved it.
        run.context["_trigger_uid"] = int(uid) if uid is not None else -1
        return int(uid) if uid is not None else None

    def get_run_channel_env(
        self,
        run_id: str,
        names: list[str] | tuple[str, ...] | None = None,
        *,
        caller_uid: int | None = None,
    ) -> dict[str, str]:
        """Return a run's NON-SECRET published ``channel_env`` references,
        filtered to the external allowlist.

        This is the read side of the git-sign bridge: an externally
        launched, workflow-gated process (a ``git`` started via ``qsu``)
        cannot inherit the run's ``SSH_AUTH_SOCK`` on its own, so the
        root-exec daemon asks for it here (carrying the run_id from the
        ``qsu --workflow-run`` handshake) and folds the result into the
        child's environment before ``exec``.

        ``caller_uid`` BINDS the lookup to the requesting uid (root-exec
        passes the qsu caller's SO_PEERCRED uid). The run is only revealed
        when its TRIGGERING process belongs to that same uid — so a run_id
        (which leaks via the WorkflowRunPending broadcast) is NOT a bearer
        capability: another uid holding the id still cannot pull the socket.
        ``caller_uid=None`` skips the binding (in-process/admin callers
        only — never the unprivileged external path).

        Fail-closed semantics — returns an EMPTY dict (never raises) when:

        - the run does not exist;
        - ``caller_uid`` is given and the run's triggering process uid does
          not match it (or cannot be determined — cron/engine-launched runs
          have no triggering process to bind to);
        - the run is not in a state where its channel is live (only a
          RUNNING run has a published channel that is still backed by a
          tracked, un-scrubbed delivery handle — a COMPLETED/FAILED run's
          channel has already been scrubbed, so handing out its socket
          path would be a stale, dangling reference);
        - the run has not yet published ``channel_env`` (the deliver_secret
          step hasn't run — the caller must retry, see
          ``wait_for_run_channel_env``);
        - a requested name is not in ``_EXTERNAL_CHANNEL_ENV_ALLOWLIST``
          (only allowlisted *references* are ever exposed; secret material
          such as the plaintext ``env`` method is never publishable and
          never reaches here).

        ``names`` optionally narrows the result to those keys (still
        intersected with the allowlist); ``None`` returns every allowlisted
        key the run has published. The value is a filesystem reference (a
        socket path), not secret material.
        """
        run = self.get_run(run_id)
        if run is None:
            return {}
        # Only a live (RUNNING) run's published reference is valid: once a
        # run COMPLETES or FAILS, _cleanup_secrets has scrubbed the agent
        # and removed the socket, so its channel_env entry is a dangling
        # path. Never hand a stale reference to a child.
        if run.state != RunState.RUNNING:
            return {}
        # Caller-uid binding (fail closed): the requesting uid must own the
        # run's triggering process. Defeats run_id-as-bearer-token.
        if caller_uid is not None:
            trig_uid = self._run_trigger_uid(run)
            if trig_uid is None or int(trig_uid) != int(caller_uid):
                return {}
        chan = run.context.get("channel_env") or {}
        if not isinstance(chan, dict) or not chan:
            return {}
        if names is None:
            wanted = _EXTERNAL_CHANNEL_ENV_ALLOWLIST
        else:
            # Intersect requested names with the allowlist — a caller can
            # never pull a non-allowlisted key by naming it explicitly.
            wanted = {n for n in names
                      if n in _EXTERNAL_CHANNEL_ENV_ALLOWLIST}
        out: dict[str, str] = {}
        for key in wanted:
            val = chan.get(key)
            # Guard against a non-string/empty value sneaking through.
            if isinstance(val, str) and val:
                out[key] = val
        return out

    def wait_for_run_channel_env(
        self,
        run_id: str,
        names: list[str] | tuple[str, ...] | None = None,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
        *,
        caller_uid: int | None = None,
    ) -> dict[str, str]:
        """Bounded wait for a run to publish its allowlisted ``channel_env``.

        The process_spawn model has an inherent ordering caveat: the run's
        ``deliver_secret`` step (which publishes ``SSH_AUTH_SOCK``) only
        fires AFTER the trigger detects the spawned process, so the
        externally launched child can reach root-exec before the run has
        published. This polls ``get_run_channel_env`` up to ``timeout``
        seconds; it returns as soon as ANY allowlisted key is available.

        Returns ``{}`` on timeout (the caller fails closed — it does NOT
        exec with a partial/stale environment). Bounded by design: never
        blocks indefinitely, and a run that fails / never publishes simply
        yields an empty dict at the deadline. ``timeout <= 0`` performs a
        single non-blocking lookup.
        """
        try:
            timeout = max(0.0, float(timeout))
        except (TypeError, ValueError):
            timeout = 0.0
        deadline = time.monotonic() + timeout
        while True:
            env = self.get_run_channel_env(run_id, names,
                                           caller_uid=caller_uid)
            if env:
                return env
            if time.monotonic() >= deadline:
                return env  # {} — fail closed
            time.sleep(max(0.01, poll_interval))

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _conditions_satisfied(self, wf: WorkflowDef,
                              run: WorkflowRun) -> tuple[bool, str]:
        """Evaluate ``wf.conditions`` (plus any ``invoker`` role) against
        the firing process. Fail-closed; empty conditions + no invoker
        role pass. See ``condition_eval`` for the matching semantics."""
        conditions = condition_eval.derive_invoker_conditions(
            wf.invoker_identities(), wf.conditions)
        try:
            return condition_eval.evaluate(conditions, run.trigger_context)
        except Exception as e:  # noqa: BLE001 — fail closed on evaluator bug
            return False, f"condition evaluation error: {e!r}"

    def _execute_steps(self, wf: WorkflowDef, run: WorkflowRun) -> None:
        """Run each step in sequence. Stop on first failure."""
        for i, step in enumerate(wf.steps):
            step_name = step.name or f"step-{i}"
            result = self._execute_step(step, run, step_name, wf)
            run.steps_completed.append(result)

            if self._audit:
                self._audit.log_step(
                    run.run_id, step_name, step.type.value,
                    result.success, result.started_at,
                    result.completed_at, result.error,
                    result.details,
                )

            if not result.success:
                error_msg = (
                    f"step {step_name} ({step.type.value}) failed: "
                    f"{result.error}"
                )
                run.mark_failed(error_msg)
                self._cleanup_secrets(run.run_id, wf.name)
                if self._audit:
                    self._audit.log_run_failed(
                        run.run_id, wf.name, error_msg,
                    )
                return

    def _execute_step(self, step: StepDef, run: WorkflowRun,
                      step_name: str,
                      wf: WorkflowDef | None = None) -> StepResult:
        """Dispatch to the appropriate step handler."""
        result = StepResult(
            step_name=step_name,
            step_type=step.type.value,
            success=False,
            started_at=time.time(),
        )
        handler_name = _STEP_HANDLER_NAMES.get(step.type)
        if handler_name is None:
            result.error = f"no handler for step type {step.type.value!r}"
            result.completed_at = time.time()
            return result

        handler = getattr(self, handler_name, None)
        if handler is None:
            result.error = f"handler method {handler_name!r} not found"
            result.completed_at = time.time()
            return result

        try:
            handler(step, run, result, wf)
        except Exception as e:  # noqa: BLE001
            result.success = False
            result.error = str(e)
        result.completed_at = time.time()
        return result

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    def _handle_deliver_secret(self, step: StepDef, run: WorkflowRun,
                               result: StepResult,
                               wf: WorkflowDef | None = None) -> None:
        """Deliver a vault secret to the workflow via a narrow channel.

        With a ``secret_source`` configured, the item is unsealed, handed
        to the chosen delivery mechanism (env/ssh-agent/fd-pass/
        tmpfs-mount), and the live handle is tracked so cleanup can
        actually revoke it. The plaintext is never logged or stored in
        the audit trail — only metadata (item name, method) is recorded.

        Without a ``secret_source`` the step runs in tracking-only mode
        (no fetch, no delivery) so the engine remains usable in tests and
        standalone smoke runs.
        """
        item = step.config.get("item", "")
        delivery_method = step.config.get("as", "env")
        scrub_on = step.config.get("scrub_on", "workflow_exit")

        if not item:
            result.error = "deliver_secret: missing 'item' in config"
            return

        # F4: a step may only deliver an item the workflow *declared* in
        # ``needs``. ``needs`` is the auditable allowlist admin approval is
        # granted against (permissions.md §"Secret delivery"); without this
        # check a workflow approved for item A could unseal item B. Fail
        # closed: an undeclared item (or an empty ``needs``) is rejected.
        if wf is not None:
            allowed = list(wf.needs or [])
            if item not in allowed:
                result.error = (
                    f"deliver_secret: item {item!r} not in workflow needs "
                    f"{allowed!r} (fail-closed)")
                result.details = {"item": item, "delivered": False}
                return

        # Track the item name for audit/scrub bookkeeping regardless of
        # whether a real backend is wired up.
        with self._secrets_lock:
            self._delivered_secrets.setdefault(run.run_id, []).append(item)

        if self._secret_source is None:
            logger.info(
                "deliver_secret: tracking-only (no secret_source) for %r "
                "as %s (scrub_on=%s) run %s",
                item, delivery_method, scrub_on, run.run_id,
            )
            result.success = True
            result.details = {
                "item": item,
                "delivery_method": delivery_method,
                "scrub_on": scrub_on,
                "delivered": False,
            }
            return

        # Real delivery. Keep the plaintext lifetime minimal.
        secret = SecretValue(self._fetch_secret(item, run))
        handle = None
        # Consumption: if an earlier step published a channel (e.g. an
        # SSH_AUTH_SOCK from a prior ssh-agent delivery) and this step
        # spawns a command, expose that channel to the child by folding it
        # into the command's environment. This is how a later step
        # actually *uses* a previously-delivered secret.
        delivery_config = dict(step.config)
        consume = step.config.get("consume_channels")
        if isinstance(consume, str):
            consume = [consume]
        channel_env = run.context.get("channel_env") or {}
        if consume and channel_env and delivery_config.get("command"):
            # Least privilege: a step only inherits the channels it
            # explicitly names, never every channel published so far — a
            # helper meant to read MARKER must not silently also receive an
            # earlier step's SSH_AUTH_SOCK.
            selected = {k: channel_env[k] for k in consume if k in channel_env}
            if selected:
                base = dict(delivery_config.get("base_env") or os.environ)
                base.update(selected)
                delivery_config["base_env"] = base
        try:
            handle = make_delivery(delivery_method, secret, delivery_config)
            handle.deliver()
        except Exception as e:  # noqa: BLE001
            # Scrub any partial channel state (open fd, mounted tmpfs,
            # started agent) AND wipe the buffer — never leave a
            # half-delivered secret behind on a failed step.
            if handle is not None:
                handle.scrub()
            else:
                secret.wipe()
            msg = e if isinstance(e, DeliveryError) else repr(e)
            result.error = f"deliver_secret: {msg}"
            return

        # Enforce scrub_on. Step-level lifetimes are revoked immediately
        # (the spawned command, if any, already ran inside deliver());
        # everything else is tracked and scrubbed at workflow exit. If the
        # engine is shutting down, scrub now rather than tracking into a
        # dict scrub_all_runs may have already drained.
        with self._secrets_lock:
            track = (scrub_on not in _STEP_SCRUB_LIFETIMES
                     and not self._stopping)
            if track:
                self._delivery_handles.setdefault(
                    run.run_id, []).append(handle)
        if not track:
            handle.scrub()
        logger.info(
            "deliver_secret: delivered %r via %s (scrub_on=%s) run %s",
            item, handle.method, scrub_on, run.run_id,
        )
        # Publish the live channel's *reference* (socket path / file path /
        # fd number — never the secret value) so a later step or the
        # triggering process can consume it. Only for tracked, command-less
        # deliveries: a step-scoped channel is already revoked, and a
        # delivery that ran its own command has nothing left to expose.
        published: list[str] = []
        if track and not step.config.get("command"):
            published = self._publish_channel(handle, step, run)
        result.success = True
        # metadata() is the only loggable surface — no secret value.
        result.details = {
            "item": item,
            "scrub_on": scrub_on,
            "delivered": True,
            **handle.metadata(),
        }
        if published:
            result.details["published"] = published

    def _publish_channel(self, handle: DeliveryHandle, step: StepDef,
                         run: WorkflowRun) -> list[str]:
        """Expose a delivered channel's non-secret reference on the run.

        Returns the published env-var names (for audit detail). The
        plaintext ``env`` method is never published — its only reference
        *is* the secret value — so it must use an inline ``command``.
        """
        env: dict[str, str] = {}
        method = handle.method
        # Only filesystem-namespace references are publishable: a socket
        # path or file path can be opened by any process granted access.
        # An fd is process-local — a later step's child can't reach an fd
        # held in the engine — so fd-pass is NOT published; it only works
        # with its own inline command.
        if method == "ssh-agent":
            sock = getattr(handle, "auth_sock", None)
            if sock:
                env[step.config.get("expose_as", "SSH_AUTH_SOCK")] = str(sock)
        elif method == "tmpfs-mount":
            path = getattr(handle, "path", None)
            if path:
                env[step.config.get("expose_as", "SECRET_PATH")] = str(path)
        if not env:
            return []
        # Steps for a single run execute sequentially on one worker thread,
        # so run.context needs no lock.
        run.context.setdefault("channel_env", {}).update(env)
        logger.info("deliver_secret: published channel %s for run %s",
                    sorted(env.keys()), run.run_id)
        return sorted(env.keys())

    def _fetch_secret(self, item: str, run: WorkflowRun) -> bytes:
        """Unseal an item via the configured secret source.

        Accepts either a callable ``fetch(item, run_id)``/``fetch(item)``
        or an object exposing ``.fetch(...)``. Returns raw bytes.
        """
        src = self._secret_source
        fetch = getattr(src, "fetch", src)
        try:
            value = fetch(item, run.run_id)
        except TypeError:
            value = fetch(item)
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        raise DeliveryError(f"secret source returned {type(value).__name__}")

    def _handle_wait_for_process(self, step: StepDef,
                                 run: WorkflowRun,
                                 result: StepResult,
                                 wf: WorkflowDef | None = None) -> None:
        """Block until a (non-child) process exits, bounded by a timeout.

        The pid is taken from ``pid``/``value`` and may be a literal or a
        ``$trigger.pid`` reference into the firing trigger's context (the
        process_spawn watcher puts the spawned pid there). Uses
        ``pidfd_open`` + ``select`` so we can wait on a process we did not
        fork; falls back to ``/proc`` polling where pidfd is unavailable.
        A timeout is a *failure* (the run shouldn't quietly proceed — and
        for git-sign this keeps the secret alive no longer than the bound).
        """
        raw_pid = step.config.get("pid", step.config.get("value", ""))
        resolved = _resolve_ref(raw_pid, run)
        try:
            pid = int(resolved)
        except (TypeError, ValueError):
            result.error = (
                f"wait_for_process: unresolved/invalid pid {raw_pid!r} "
                f"(resolved={resolved!r})")
            return
        if pid <= 0:
            result.error = f"wait_for_process: invalid pid {pid}"
            return

        timeout = _clamp_timeout(step.config.get("timeout"),
                                 _DEFAULT_WAIT_TIMEOUT_S,
                                 _MAX_WAIT_TIMEOUT_S)
        # Anchor against PID reuse: prefer the starttime the trigger
        # captured at spawn, else read it now. If the live process no
        # longer matches this anchor, the original has exited (and the PID
        # was recycled) — we must not keep waiting (and keep the secret
        # alive) on an unrelated process.
        expected_start = run.trigger_context.get("pid_starttime")
        if expected_start is None:
            expected_start = _proc_starttime(pid)
        logger.info("wait_for_process: waiting for pid=%s (<=%.0fs) run %s",
                    pid, timeout, run.run_id)
        exited = self._wait_pid(pid, timeout, expected_start)
        result.details = {"pid": pid, "timeout_s": timeout, "exited": exited}
        if exited:
            result.success = True
        else:
            result.error = f"wait_for_process: pid {pid} still alive after {timeout}s"

    @staticmethod
    def _wait_pid(pid: int, timeout: float,
                  expected_start: int | None = None) -> bool:
        """Return True if ``pid`` (the *original* process) exits in time.

        ``expected_start`` is the process's /proc starttime, used as an
        anti-PID-reuse anchor on the polling fallback: if the live pid's
        starttime ever differs, the original is gone and the pid was
        recycled, so we report exit rather than wait on a stranger.
        """
        deadline = time.monotonic() + timeout
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is not None:
            try:
                fd = pidfd_open(pid)
            except ProcessLookupError:
                return True  # already gone
            except OSError:
                fd = None
            if fd is not None:
                try:
                    poller = select.poll()
                    poller.register(fd, select.POLLIN)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        # poll() takes milliseconds.
                        if poller.poll(min(remaining, 1.0) * 1000.0):
                            return True
                finally:
                    os.close(fd)
        # Fallback: poll /proc liveness, re-checking the starttime anchor
        # so a recycled pid is treated as "original exited".
        def _original_gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass  # exists, foreign uid
            if expected_start is not None:
                now_start = _proc_starttime(pid)
                if now_start is not None and now_start != expected_start:
                    return True
            return False

        while time.monotonic() < deadline:
            if _original_gone():
                return True
            time.sleep(0.1)
        return _original_gone()

    def _handle_run_hook(self, step: StepDef, run: WorkflowRun,
                         result: StepResult,
                         wf: WorkflowDef | None = None) -> None:
        """Execute a sandboxed broker hook and map its verdict to outcome.

        The step's ``hook`` names the action and ``event`` carries the
        payload dict handed to the executor. Verdict mapping:
        ``allow``/``transform`` → success, ``deny`` → failure, a missing
        verdict / unreachable executor (``null``) → failure (fail closed:
        a step that can't confirm its hook ran must not report success).
        """
        hook_name = str(step.config.get("hook",
                                        step.config.get("name", ""))).strip()
        if not hook_name:
            result.error = "run_hook: missing 'hook' name in config"
            return
        event = step.config.get("event", {})
        if not isinstance(event, dict):
            event = {"value": event}

        client = getattr(self._broker, "hooks", None)
        if client is None:
            result.error = "run_hook: no hook client available"
            return

        verdict = client.query(hook_name, dict(event))
        if not isinstance(verdict, dict):
            # None == null/unreachable/timeout/protocol error.
            result.error = f"run_hook: hook {hook_name!r} returned no verdict"
            result.details = {"hook": hook_name, "verdict": None}
            return
        decision = str(verdict.get("verdict", "")).lower()
        result.details = {"hook": hook_name, "verdict": decision}
        if "reason" in verdict:
            # Cap the hook-supplied reason: it lands in the audit DB, and a
            # hook could otherwise pour an unbounded payload into it.
            result.details["reason"] = str(verdict["reason"])[:512]
        if decision in ("allow", "transform"):
            result.success = True
        else:
            result.error = (
                f"run_hook: hook {hook_name!r} verdict={decision or 'unknown'}")

    def _handle_call_dbus(self, step: StepDef, run: WorkflowRun,
                          result: StepResult,
                          wf: WorkflowDef | None = None) -> None:
        """Call an arbitrary D-Bus method via a proxy, bounded + isolated.

        Config: ``bus`` (system|session, default system), ``bus_name``,
        ``object_path``, ``interface``, ``method``, ``args`` (list),
        ``timeout``. Any exception is contained as a step failure.
        """
        bus_kind = str(step.config.get("bus", "system"))
        bus_name = str(step.config.get("bus_name", ""))
        obj_path = str(step.config.get("object_path", ""))
        interface = str(step.config.get("interface", "")) or None
        method = str(step.config.get("method", ""))
        args = step.config.get("args", [])
        if isinstance(args, dict) or not isinstance(args, (list, tuple)):
            args = [args] if args != {} and args != [] else []
        if not (bus_name and obj_path and method):
            result.error = ("call_dbus: bus_name, object_path and method "
                            "are all required")
            return
        if method.startswith("_"):
            result.error = f"call_dbus: refusing private method {method!r}"
            return
        # F2: this runs as root inside the broker. When hardening is enabled
        # (deliberately OFF by default for now — see
        # _dbus_hardening_enabled / the dbus-hardening todo), refuse the
        # privilege-escalation/secret bus names outright and, when an
        # allowlist is configured, refuse anything not on it (default deny).
        # Without this, a workflow YAML is an arbitrary root D-Bus escape
        # hatch — intentionally tolerated during testing only.
        if _dbus_hardening_enabled():
            # Reject unique connection names (":1.N"): the denylist /
            # allowlist match well-known names, but a workflow could
            # otherwise name the *unique* owner of a denied service (e.g.
            # systemd1's ":1.3") and slip past the well-known-name check.
            if bus_name.startswith(":"):
                result.error = (
                    f"call_dbus: refusing unique connection name "
                    f"{bus_name!r}; address services by well-known name")
                return
            if bus_name in _DBUS_BUS_NAME_DENYLIST:
                result.error = (
                    f"call_dbus: bus_name {bus_name!r} is denied "
                    f"(use call_broker for the broker; the vault/init/login "
                    f"managers are never callable from a workflow)")
                return
            allow = _dbus_allowlist()
            if allow and bus_name not in allow:
                result.error = (
                    f"call_dbus: bus_name {bus_name!r} not in the configured "
                    f"allowlist {sorted(allow)} (fail-closed)")
                return
        timeout = _clamp_timeout(step.config.get("timeout"),
                                 _DEFAULT_DBUS_TIMEOUT_S, _MAX_DBUS_TIMEOUT_S)

        try:
            import dbus  # lazy
            bus = (dbus.SessionBus() if bus_kind == "session"
                   else dbus.SystemBus())
            # introspect=False keeps name resolution/introspection from
            # running an unbounded round-trip outside the call timeout: a
            # broken/hostile service can't stall the worker before the
            # bounded method call. Activation on first call is still
            # bounded by the same timeout.
            obj = bus.get_object(bus_name, obj_path, introspect=False)
            fn = obj.get_dbus_method(method, dbus_interface=interface)
            ret = fn(*args, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            result.error = f"call_dbus: {e!r}"
            result.details = {
                "bus_name": bus_name, "object_path": obj_path,
                "interface": interface or "", "method": method,
            }
            return
        result.success = True
        # Never persist the return payload: an arbitrary D-Bus method may
        # return secret text or byte data, and step details are written to
        # the audit DB. Record only its shape.
        result.details = {
            "bus_name": bus_name, "object_path": obj_path,
            "interface": interface or "", "method": method,
            **_return_shape(ret),
        }

    def _handle_call_broker(self, step: StepDef, run: WorkflowRun,
                            result: StepResult,
                            wf: WorkflowDef | None = None) -> None:
        """Invoke a whitelisted method on the in-process broker proxy.

        Only side-effect-light read/maintenance methods are permitted
        (see ``_BROKER_METHOD_WHITELIST``); anything else fails closed.
        """
        method = str(step.config.get("method", ""))
        args = step.config.get("args", [])
        if not isinstance(args, (list, tuple)):
            args = [args]
        if self._broker is None:
            result.error = "call_broker: no broker proxy available"
            return
        if method not in _BROKER_METHOD_WHITELIST:
            result.error = (
                f"call_broker: method {method!r} not in whitelist "
                f"{sorted(_BROKER_METHOD_WHITELIST)}")
            return
        fn = getattr(self._broker, method, None)
        if not callable(fn):
            result.error = f"call_broker: method {method!r} not callable"
            return
        try:
            ret = fn(*args)
        except Exception as e:  # noqa: BLE001
            result.error = f"call_broker: {method} raised {e!r}"
            result.details = {"method": method}
            return
        result.success = True
        # Shape only — broker list methods can surface action details we
        # don't want copied into the workflow audit trail.
        result.details = {"method": method, **_return_shape(ret)}

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _cleanup_secrets(self, run_id: str, workflow_name: str) -> None:
        """Scrub every secret delivered during this run.

        Called on both success and failure (including mid-step failure).
        Each live delivery handle is revoked (agent killed, fds closed,
        tmpfs unmounted, buffer wiped); the scrub is audited by item name
        only — never the secret value.
        """
        with self._secrets_lock:
            handles = self._delivery_handles.pop(run_id, [])
            secrets = self._delivered_secrets.pop(run_id, [])
        for handle in handles:
            try:
                handle.scrub()
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "secret scrub failed for run %s (%s): %r",
                    run_id, handle.method, e,
                )
        for item in secrets:
            logger.info("scrubbed secret %r from run %s", item, run_id)
            if self._audit:
                self._audit.log_secret_scrub(run_id, workflow_name, item)

    def scrub_all_runs(self) -> None:
        """Revoke every outstanding delivery across all runs.

        Best-effort safety net for shutdown so a secret never outlives
        the engine process when a clean per-run scrub was missed.
        """
        with self._secrets_lock:
            run_ids = list(self._delivery_handles.keys())
        with self._runs_lock:
            names = {rid: self._runs[rid].workflow_name
                     for rid in run_ids if rid in self._runs}
        for run_id in run_ids:
            self._cleanup_secrets(run_id, names.get(run_id, ""))

    # ------------------------------------------------------------------
    # Trigger callback
    # ------------------------------------------------------------------

    @staticmethod
    def _dedup_key(workflow_name: str,
                   trigger_context: dict[str, Any]) -> tuple[str, str] | None:
        """A (workflow, cgroup) key for collapsing duplicate process_spawn
        fires, or None when dedup doesn't apply (no cgroup in context)."""
        cgroup = trigger_context.get("cgroup")
        if cgroup is None:
            return None
        return (workflow_name, str(cgroup))

    def _on_trigger(self, workflow_name: str,
                    trigger_context: dict[str, Any]) -> None:
        """Called by the trigger registry when a trigger fires.

        Applies the auto-run gate (F3), per-cgroup dedup (F6) and the
        in-flight bound (F8), then dispatches to the worker pool and
        returns immediately so the firing thread (a watcher thread, or the
        broker main loop for D-Bus triggers) is never occupied by
        workflow execution.
        """
        wf = self._workflows.get(workflow_name)
        if wf is None:
            logger.warning("trigger fired for unknown workflow %s",
                           workflow_name)
            return

        # F3: human-in-the-loop default. A workflow only auto-executes if
        # it opted in with ``auto_run: true``; otherwise the fire records a
        # PENDING run awaiting admin approval.
        if not wf.auto_run:
            self._enqueue_pending(workflow_name, trigger_context)
            return

        dedup_key = self._dedup_key(workflow_name, trigger_context)
        with self._active_lock:
            if self._inflight >= self._max_inflight:
                logger.warning(
                    "workflow run queue saturated (%d in flight); dropping "
                    "trigger for %s", self._inflight, workflow_name)
                return
            if dedup_key is not None and dedup_key in self._active_keys:
                logger.info(
                    "collapsing duplicate trigger for %s (cgroup %s already "
                    "has an active run)", workflow_name, dedup_key[1])
                return
            if dedup_key is not None:
                self._active_keys.add(dedup_key)
            self._inflight += 1

        logger.info("trigger fired for workflow %s, dispatching run",
                    workflow_name)
        try:
            self._run_pool.submit(self._run_from_trigger,
                                  workflow_name, trigger_context, dedup_key)
        except RuntimeError as e:
            # Pool already shut down (engine stopping) — drop the fire and
            # release the accounting we just took.
            self._release_inflight(dedup_key)
            logger.warning("dropping trigger for %s: %r", workflow_name, e)

    def _release_inflight(self, dedup_key: tuple[str, str] | None) -> None:
        with self._active_lock:
            self._inflight = max(0, self._inflight - 1)
            if dedup_key is not None:
                self._active_keys.discard(dedup_key)

    def _run_from_trigger(self, workflow_name: str,
                          trigger_context: dict[str, Any],
                          dedup_key: tuple[str, str] | None = None) -> None:
        try:
            self.start_run(workflow_name, trigger_context)
        except Exception as e:  # noqa: BLE001
            logger.error("failed to start run for workflow %s: %s",
                         workflow_name, e)
        finally:
            self._release_inflight(dedup_key)

    # ------------------------------------------------------------------
    # Approval queue (F3)
    # ------------------------------------------------------------------

    def _enqueue_pending(self, workflow_name: str,
                         trigger_context: dict[str, Any]) -> WorkflowRun:
        """Record a PENDING run that an admin must approve before it runs.

        The trigger context is captured so the eventual approved run sees
        the same firing process (conditions are re-checked at execution).
        """
        # Don't flood the queue: a periodic trigger (cron) that keeps
        # firing while its previous run is still awaiting approval must not
        # accumulate a new PENDING run every tick. Collapse to the existing
        # pending run for the same (workflow, cgroup).
        dedup_key = self._dedup_key(workflow_name, trigger_context)
        with self._runs_lock:
            for existing in self._runs.values():
                if (existing.state == RunState.PENDING
                        and existing.workflow_name == workflow_name
                        and self._dedup_key(
                            workflow_name, existing.trigger_context)
                        == dedup_key):
                    logger.info(
                        "workflow %s already has a pending run %s; not "
                        "enqueuing another", workflow_name, existing.run_id)
                    return existing

        run = WorkflowRun(
            workflow_name=workflow_name,
            trigger_context=dict(trigger_context or {}),
        )
        # state defaults to PENDING.
        with self._runs_lock:
            self._runs[run.run_id] = run
        if self._audit:
            self._audit.log_run_pending(
                run.run_id, workflow_name, run.trigger_context)
        logger.info("workflow %s run %s PENDING admin approval",
                    workflow_name, run.run_id)
        # Best-effort: nudge the admin UI / approval queue.
        emit = getattr(self._broker, "WorkflowRunPending", None)
        if callable(emit):
            try:
                emit(run.run_id, workflow_name)
            except Exception as e:  # noqa: BLE001
                logger.debug("WorkflowRunPending emit failed: %r", e)
        return run

    def list_pending_runs(self) -> list[WorkflowRun]:
        with self._runs_lock:
            return [r for r in self._runs.values()
                    if r.state == RunState.PENDING]

    def approve_run(self, run_id: str) -> bool:
        """Approve a PENDING run and dispatch it for execution.

        Returns True if a pending run was found and scheduled. The run is
        executed on the worker pool (conditions are re-evaluated there, so
        approval does not bypass the identity gate).
        """
        with self._runs_lock:
            run = self._runs.get(run_id)
            if run is None or run.state != RunState.PENDING:
                return False
            # Flip out of PENDING immediately so a double-approve can't
            # schedule the same run twice.
            run.mark_running()
        with self._active_lock:
            if self._inflight >= self._max_inflight:
                logger.warning("queue saturated; cannot approve run %s now",
                               run_id)
                run.state = RunState.PENDING
                return False
            self._inflight += 1
        try:
            self._run_pool.submit(self._run_approved, run)
        except RuntimeError as e:
            self._release_inflight(None)
            run.state = RunState.PENDING
            logger.warning("cannot approve run %s (pool down): %r", run_id, e)
            return False
        logger.info("approved workflow run %s (%s)", run_id, run.workflow_name)
        return True

    def _run_approved(self, run: WorkflowRun) -> None:
        """Execute an already-created (approved) run in place."""
        try:
            self._execute_existing_run(run)
        except Exception as e:  # noqa: BLE001
            logger.error("approved run %s failed: %s", run.run_id, e)
        finally:
            self._release_inflight(None)


# Step handler dispatch table — maps step types to method names.
# Uses method names (not bound methods) so subclasses can override
# individual handlers for testing.
_STEP_HANDLER_NAMES: dict[StepType, str] = {
    StepType.DELIVER_SECRET: "_handle_deliver_secret",
    StepType.WAIT_FOR_PROCESS: "_handle_wait_for_process",
    StepType.RUN_HOOK: "_handle_run_hook",
    StepType.CALL_DBUS: "_handle_call_dbus",
    StepType.CALL_BROKER: "_handle_call_broker",
}
