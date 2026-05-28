"""Data models for workflow definitions and run records.

Uses plain dataclasses (no Pydantic dependency) to stay consistent
with the rest of qdistro's broker-side code. All fields are
validated at construction time by the loader; the dataclasses
themselves are intentionally permissive so the engine can build
partial objects during error-recovery paths.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TriggerType(str, Enum):
    PROCESS_SPAWN = "process_spawn"
    CRON = "cron"
    DBUS_SIGNAL = "dbus_signal"
    QBUS_EVENT = "qbus_event"


class StepType(str, Enum):
    DELIVER_SECRET = "deliver_secret"
    WAIT_FOR_PROCESS = "wait_for_process"
    RUN_HOOK = "run_hook"
    CALL_DBUS = "call_dbus"
    CALL_BROKER = "call_broker"


class RunState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass
class TriggerDef:
    """A workflow trigger definition."""
    type: TriggerType
    config: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TriggerDef:
        if not isinstance(data, dict):
            raise ValueError(f"trigger must be a mapping, got {type(data).__name__}")
        raw_type = data.get("type")
        if raw_type is None:
            raise ValueError("trigger must have a 'type' field")
        try:
            trigger_type = TriggerType(str(raw_type))
        except ValueError:
            valid = [t.value for t in TriggerType]
            raise ValueError(
                f"trigger.type must be one of {valid}, got {raw_type!r}"
            )
        config = {k: v for k, v in data.items() if k != "type"}
        return TriggerDef(type=trigger_type, config=config)


@dataclass
class StepDef:
    """A single workflow step."""
    type: StepType
    name: str = ""
    config: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> StepDef:
        if not isinstance(data, dict):
            raise ValueError(f"step must be a mapping, got {type(data).__name__}")
        # Steps can be specified as {"deliver_secret": {config...}} shorthand
        # or as {"type": "deliver_secret", ...} longhand.
        raw_type = data.get("type")
        step_name = str(data.get("name", ""))
        config: dict[str, Any] = {}
        if raw_type is not None:
            # Longhand: {"type": "run_hook", "config_key": "val"}
            config = {k: v for k, v in data.items()
                      if k not in ("type", "name")}
        else:
            # Shorthand: {"deliver_secret": {"item": "vault/..."}}
            step_type_values = {t.value for t in StepType}
            found = None
            for k, v in data.items():
                if k == "name":
                    continue
                if k in step_type_values:
                    found = k
                    config = dict(v) if isinstance(v, dict) else {"value": v}
                    break
            if found is None:
                raise ValueError(
                    f"step must have a 'type' field or a step-type key "
                    f"({sorted(step_type_values)}), got keys {sorted(data.keys())}"
                )
            raw_type = found
        try:
            step_type = StepType(str(raw_type))
        except ValueError:
            valid = [t.value for t in StepType]
            raise ValueError(
                f"step type must be one of {valid}, got {raw_type!r}"
            )
        return StepDef(type=step_type, name=step_name, config=config)


@dataclass
class WorkflowDef:
    """A complete workflow definition loaded from YAML."""
    name: str
    trigger: TriggerDef
    steps: list[StepDef]
    conditions: list[dict[str, Any]] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    # Human-in-the-loop is the default (permissions.md §Principles): a
    # loaded workflow does NOT auto-execute on a trigger fire unless it
    # explicitly opts in with ``auto_run: true``. Otherwise the fire
    # creates a PENDING run that an admin must approve.
    auto_run: bool = False
    source_path: str = ""
    description: str = ""

    def invoker_identities(self) -> list[str]:
        """Roles whose value is ``invoker`` — the identities allowed to
        invoke this workflow. ``roles: {dev: invoker}`` -> ``["dev"]``.
        Used to derive a fail-closed uid constraint when ``conditions``
        does not pin one explicitly."""
        return [k for k, v in self.roles.items()
                if str(v).strip().lower() == "invoker"]

    def approver_identities(self) -> list[str]:
        """Roles whose value is ``approver`` — identities allowed to
        approve a pending run of this workflow."""
        return [k for k, v in self.roles.items()
                if str(v).strip().lower() == "approver"]

    @staticmethod
    def from_dict(data: dict[str, Any], source_path: str = "") -> WorkflowDef:
        if not isinstance(data, dict):
            raise ValueError(
                f"workflow must be a mapping, got {type(data).__name__}"
            )
        name = data.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("workflow must have a non-empty 'name' string")
        trigger_raw = data.get("trigger")
        if trigger_raw is None:
            raise ValueError("workflow must have a 'trigger' field")
        trigger = TriggerDef.from_dict(trigger_raw)

        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or len(steps_raw) == 0:
            raise ValueError("workflow must have a non-empty 'steps' list")
        steps = [StepDef.from_dict(s) for s in steps_raw]

        conditions = list(data.get("conditions") or [])
        for i, c in enumerate(conditions):
            if not isinstance(c, dict):
                raise ValueError(
                    f"conditions[{i}] must be a mapping, "
                    f"got {type(c).__name__}"
                )

        needs_raw = data.get("needs") or []
        # Support scalar shorthand: `needs: vault/dev/key` -> ["vault/dev/key"]
        if isinstance(needs_raw, str):
            needs_raw = [needs_raw]
        if not isinstance(needs_raw, list):
            raise ValueError(
                f"needs must be a list or string, "
                f"got {type(needs_raw).__name__}"
            )
        needs = list(needs_raw)
        for i, n in enumerate(needs):
            if not isinstance(n, str):
                raise ValueError(
                    f"needs[{i}] must be a string, got {type(n).__name__}"
                )

        roles_raw = data.get("roles") or {}
        if isinstance(roles_raw, list):
            # Support list-of-dicts format from the design doc:
            #   roles:
            #     - dev: invoker
            #     - admin: approver
            roles = {}
            for item in roles_raw:
                if isinstance(item, dict):
                    roles.update({str(k): str(v) for k, v in item.items()})
        elif isinstance(roles_raw, dict):
            roles = {str(k): str(v) for k, v in roles_raw.items()}
        else:
            raise ValueError(
                f"roles must be a mapping or list, "
                f"got {type(roles_raw).__name__}"
            )

        auto_run_raw = data.get("auto_run", False)
        if not isinstance(auto_run_raw, bool):
            raise ValueError(
                f"auto_run must be a boolean, got {type(auto_run_raw).__name__}"
            )

        unknown = set(data.keys()) - {
            "name", "trigger", "steps", "conditions",
            "needs", "roles", "auto_run", "description",
        }
        if unknown:
            raise ValueError(f"unknown top-level keys: {sorted(unknown)}")

        return WorkflowDef(
            name=str(name),
            trigger=trigger,
            steps=steps,
            conditions=conditions,
            needs=needs,
            roles=roles,
            auto_run=bool(auto_run_raw),
            source_path=source_path,
            description=str(data.get("description") or ""),
        )


@dataclass
class StepResult:
    """Outcome of executing a single step."""
    step_name: str
    step_type: str
    success: bool
    started_at: float = 0.0
    completed_at: float = 0.0
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowRun:
    """A single execution of a workflow."""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    workflow_name: str = ""
    state: RunState = RunState.PENDING
    started_at: float = 0.0
    completed_at: float = 0.0
    steps_completed: list[StepResult] = field(default_factory=list)
    trigger_context: dict[str, Any] = field(default_factory=dict)
    # Non-secret channel references published by deliver_secret steps for
    # later steps / the triggering process to consume (e.g.
    # {"channel_env": {"SSH_AUTH_SOCK": "/run/.../agent.sock"}}). Never
    # holds plaintext secret values, and is not written to the audit DB.
    context: dict[str, Any] = field(default_factory=dict)
    audit_entries: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def mark_running(self) -> None:
        self.state = RunState.RUNNING
        self.started_at = time.time()

    def mark_completed(self) -> None:
        self.state = RunState.COMPLETED
        self.completed_at = time.time()

    def mark_failed(self, error: str) -> None:
        self.state = RunState.FAILED
        self.completed_at = time.time()
        self.error = error
