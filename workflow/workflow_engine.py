"""Core workflow engine.

Orchestrates workflow execution: loads definitions, registers
triggers, executes steps in order, audits each step, and handles
failure cleanup (secret scrubbing, run-state marking).
"""
from __future__ import annotations

import concurrent.futures
import logging
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
        # Stop accepting new runs and cancel any not-yet-started ones;
        # in-flight runs finish (and self-scrub) on their worker threads.
        try:
            self._run_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # pragma: no cover - py<3.9
            self._run_pool.shutdown(wait=False)
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
        # everything else is tracked and scrubbed at workflow exit.
        if scrub_on in _STEP_SCRUB_LIFETIMES:
            handle.scrub()
        else:
            self._delivery_handles.setdefault(run.run_id, []).append(handle)
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
        """Wait for a process to exit.

        Skeleton: logs and succeeds immediately. Production would
        poll /proc/<pid> or use waitid/pidfd.
        """
        pid_ref = step.config.get("pid", step.config.get("value", ""))
        logger.info(
            "wait_for_process: would wait for pid=%s in run %s",
            pid_ref, run.run_id,
        )
        result.success = True
        result.details = {"pid_ref": str(pid_ref)}

    def _handle_run_hook(self, step: StepDef, run: WorkflowRun,
                         result: StepResult) -> None:
        """Run a broker hook.

        Skeleton: logs the hook name. Production would call through
        HookClient or the hook executor directly.
        """
        hook_name = step.config.get("hook", step.config.get("name", ""))
        hook_event = step.config.get("event", {})
        logger.info(
            "run_hook: would execute hook %r for run %s",
            hook_name, run.run_id,
        )
        result.success = True
        result.details = {"hook": hook_name}

    def _handle_call_dbus(self, step: StepDef, run: WorkflowRun,
                          result: StepResult) -> None:
        """Call a D-Bus method.

        Skeleton: logs the method details. Production would call the
        method via the D-Bus proxy.
        """
        bus_name = step.config.get("bus_name", "")
        obj_path = step.config.get("object_path", "")
        interface = step.config.get("interface", "")
        method = step.config.get("method", "")
        logger.info(
            "call_dbus: would call %s.%s on %s%s for run %s",
            interface, method, bus_name, obj_path, run.run_id,
        )
        result.success = True
        result.details = {
            "bus_name": bus_name,
            "object_path": obj_path,
            "interface": interface,
            "method": method,
        }

    def _handle_call_broker(self, step: StepDef, run: WorkflowRun,
                            result: StepResult) -> None:
        """Call a broker method.

        Skeleton: logs the method name. Production would call via
        the broker D-Bus proxy.
        """
        method = step.config.get("method", "")
        args = step.config.get("args", {})
        logger.info(
            "call_broker: would call broker method %r for run %s",
            method, run.run_id,
        )
        result.success = True
        result.details = {"method": method, "args": args}

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
        handles = self._delivery_handles.pop(run_id, [])
        for handle in handles:
            try:
                handle.scrub()
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "secret scrub failed for run %s (%s): %r",
                    run_id, handle.method, e,
                )
        secrets = self._delivered_secrets.pop(run_id, [])
        for item in secrets:
            logger.info("scrubbed secret %r from run %s", item, run_id)
            if self._audit:
                self._audit.log_secret_scrub(run_id, workflow_name, item)

    def scrub_all_runs(self) -> None:
        """Revoke every outstanding delivery across all runs.

        Best-effort safety net for shutdown so a secret never outlives
        the engine process when a clean per-run scrub was missed.
        """
        for run_id in list(self._delivery_handles.keys()):
            self._cleanup_secrets(run_id, self._runs.get(
                run_id, WorkflowRun()).workflow_name)

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
