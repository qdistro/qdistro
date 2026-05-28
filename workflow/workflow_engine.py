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


def _coerce_dbus(value: Any, _depth: int = 0) -> Any:
    """Convert a D-Bus / broker return into plain, JSON-able, bounded data.

    Keeps the audit detail small and serialisable: dbus numeric/string
    types become Python builtins, containers are capped, and over-long
    strings are truncated. Never raises.
    """
    try:
        if _depth > 4:
            return "..."
        if isinstance(value, bool):
            return bool(value)
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return float(value)
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if isinstance(value, str):
            return value if len(value) <= 512 else value[:512] + "...[truncated]"
        if isinstance(value, dict):
            return {str(_coerce_dbus(k, _depth + 1)): _coerce_dbus(v, _depth + 1)
                    for k, v in list(value.items())[:50]}
        if isinstance(value, (list, tuple)):
            return [_coerce_dbus(v, _depth + 1) for v in list(value)[:50]]
        if value is None:
            return None
        return str(value)
    except Exception:  # noqa: BLE001
        return "<uncoercible>"


def _resolve_ref(value: Any, trigger_context: dict[str, Any]) -> Any:
    """Resolve a ``$trigger.<key>`` reference against the trigger context.

    A plain (non-string, or non-``$trigger.``) value is returned as-is, so
    a step can pass a literal pid as easily as ``$trigger.pid``.
    """
    if isinstance(value, str):
        for prefix in ("$trigger.", "trigger."):
            if value.startswith(prefix):
                return trigger_context.get(value[len(prefix):])
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
        self._workflows: dict[str, WorkflowDef] = {}
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
        """
        # Unregister triggers for workflows that were removed.
        current_names = set(self._workflows.keys())
        stale_names = set(self._registry.active_triggers().keys()) - current_names
        for name in stale_names:
            self._registry.unregister(name)
            logger.info("unregistered stale trigger for removed workflow %s",
                        name)

        for wf in self._workflows.values():
            try:
                self._registry.register(wf, self._on_trigger)
                logger.info(
                    "registered trigger for workflow %s (type=%s)",
                    wf.name, wf.trigger.type.value,
                )
            except Exception as e:  # noqa: BLE001
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

        if self._audit:
            self._audit.log_run_start(
                run.run_id, workflow_name, run.trigger_context,
            )

        logger.info(
            "starting workflow run %s for %s",
            run.run_id, workflow_name,
        )

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
    # Step execution
    # ------------------------------------------------------------------

    def _execute_steps(self, wf: WorkflowDef, run: WorkflowRun) -> None:
        """Run each step in sequence. Stop on first failure."""
        for i, step in enumerate(wf.steps):
            step_name = step.name or f"step-{i}"
            result = self._execute_step(step, run, step_name)
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
                      step_name: str) -> StepResult:
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
            handler(step, run, result)
        except Exception as e:  # noqa: BLE001
            result.success = False
            result.error = str(e)
        result.completed_at = time.time()
        return result

    # ------------------------------------------------------------------
    # Step handlers
    # ------------------------------------------------------------------

    def _handle_deliver_secret(self, step: StepDef, run: WorkflowRun,
                               result: StepResult) -> None:
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
        try:
            handle = make_delivery(delivery_method, secret, dict(step.config))
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
        result.success = True
        # metadata() is the only loggable surface — no secret value.
        result.details = {
            "item": item,
            "scrub_on": scrub_on,
            "delivered": True,
            **handle.metadata(),
        }

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
                                 result: StepResult) -> None:
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
        resolved = _resolve_ref(raw_pid, run.trigger_context)
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
        logger.info("wait_for_process: waiting for pid=%s (<=%.0fs) run %s",
                    pid, timeout, run.run_id)
        exited = self._wait_pid(pid, timeout)
        result.details = {"pid": pid, "timeout_s": timeout, "exited": exited}
        if exited:
            result.success = True
        else:
            result.error = f"wait_for_process: pid {pid} still alive after {timeout}s"

    @staticmethod
    def _wait_pid(pid: int, timeout: float) -> bool:
        """Return True if ``pid`` exits within ``timeout`` seconds."""
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
        # Fallback: poll /proc liveness via signal 0.
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                # Exists but owned by another uid — still "alive".
                pass
            time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        return False

    def _handle_run_hook(self, step: StepDef, run: WorkflowRun,
                         result: StepResult) -> None:
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
            result.details["reason"] = str(verdict["reason"])
        if decision in ("allow", "transform"):
            result.success = True
        else:
            result.error = (
                f"run_hook: hook {hook_name!r} verdict={decision or 'unknown'}")

    def _handle_call_dbus(self, step: StepDef, run: WorkflowRun,
                          result: StepResult) -> None:
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
        timeout = _clamp_timeout(step.config.get("timeout"),
                                 _DEFAULT_DBUS_TIMEOUT_S, _MAX_DBUS_TIMEOUT_S)

        try:
            import dbus  # lazy
            bus = (dbus.SessionBus() if bus_kind == "session"
                   else dbus.SystemBus())
            obj = bus.get_object(bus_name, obj_path)
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
        result.details = {
            "bus_name": bus_name, "object_path": obj_path,
            "interface": interface or "", "method": method,
            "returned": _coerce_dbus(ret),
        }

    def _handle_call_broker(self, step: StepDef, run: WorkflowRun,
                            result: StepResult) -> None:
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
        result.details = {"method": method, "returned": _coerce_dbus(ret)}

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

    def _on_trigger(self, workflow_name: str,
                    trigger_context: dict[str, Any]) -> None:
        """Called by the trigger registry when a trigger fires.

        Dispatches the run to the worker pool and returns immediately so
        the firing thread (a watcher thread, or the broker main loop for
        D-Bus triggers) is never occupied by workflow execution.
        """
        logger.info("trigger fired for workflow %s, dispatching run",
                    workflow_name)
        try:
            self._run_pool.submit(self._run_from_trigger,
                                  workflow_name, trigger_context)
        except RuntimeError as e:
            # Pool already shut down (engine stopping) — drop the fire.
            logger.warning("dropping trigger for %s: %r", workflow_name, e)

    def _run_from_trigger(self, workflow_name: str,
                          trigger_context: dict[str, Any]) -> None:
        try:
            self.start_run(workflow_name, trigger_context)
        except Exception as e:  # noqa: BLE001
            logger.error("failed to start run for workflow %s: %s",
                         workflow_name, e)


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
