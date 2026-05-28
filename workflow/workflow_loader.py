"""YAML loader for workflow definitions.

Reads workflow YAML files from system and user directories,
validates them against the schema, and returns a list of
WorkflowDef objects. Follows the same patterns as
qdistro_admin_rules.py: sorted file order, per-file size caps,
graceful degradation on parse errors.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from workflow_schema import WorkflowDef  # type: ignore[import-not-found]

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Default directories for workflow definitions.
# System-wide workflows go in /etc/qdistro/workflows/;
# per-user overrides in ~/.config/qdistro/workflows/.
SYSTEM_WORKFLOW_DIR = "/etc/qdistro/workflows"
USER_WORKFLOW_DIR_RELATIVE = ".config/qdistro/workflows"

# Same size caps as rules engine — generous for YAML, hard ceiling.
MAX_FILE_BYTES = 1 * 1024 * 1024       # 1 MiB per file
MAX_TOTAL_BYTES = 8 * 1024 * 1024      # 8 MiB across all files


class WorkflowLoader:
    """Load and validate workflow definitions from disk."""

    def __init__(
        self,
        system_dir: str = SYSTEM_WORKFLOW_DIR,
        user_dir: str | None = None,
    ):
        self._system_dir = system_dir
        if user_dir is not None:
            self._user_dir = user_dir
        else:
            home = os.environ.get("HOME", "")
            self._user_dir = (
                os.path.join(home, USER_WORKFLOW_DIR_RELATIVE) if home else ""
            )
        self._workflows: list[WorkflowDef] = []
        self._errors: list[str] = []

    def load(self) -> list[WorkflowDef]:
        """Load all workflow definitions. Returns the list of valid
        workflows. Parse errors are accumulated and available via
        ``load_errors()``."""
        self._workflows = []
        self._errors = []

        if yaml is None:
            self._errors.append("PyYAML not installed; no workflows loaded")
            return []

        # System-wide workflows first, then user workflows.
        # User workflows with the same name override system ones.
        system_wfs = self._load_dir(self._system_dir)
        user_wfs = self._load_dir(self._user_dir)

        # Merge: user workflows override system workflows by name.
        by_name: dict[str, WorkflowDef] = {}
        for wf in system_wfs:
            by_name[wf.name] = wf
        for wf in user_wfs:
            by_name[wf.name] = wf

        self._workflows = list(by_name.values())
        return list(self._workflows)

    def reload(self) -> list[WorkflowDef]:
        """Convenience alias for load()."""
        return self.load()

    def load_errors(self) -> list[str]:
        return list(self._errors)

    def workflows(self) -> list[WorkflowDef]:
        return list(self._workflows)

    def _load_dir(self, directory: str) -> list[WorkflowDef]:
        """Load all YAML files from a single directory."""
        if not directory or not os.path.isdir(directory):
            return []

        workflows: list[WorkflowDef] = []
        total_bytes = 0

        for name in sorted(os.listdir(directory)):
            if not (name.endswith(".yaml") or name.endswith(".yml")):
                continue
            path = os.path.join(directory, name)

            try:
                size = os.path.getsize(path)
            except OSError as e:
                self._errors.append(f"{path}: stat failed: {e}")
                continue

            if size > MAX_FILE_BYTES:
                self._errors.append(
                    f"{path}: {size} bytes exceeds per-file cap "
                    f"{MAX_FILE_BYTES}; skipping"
                )
                continue
            if total_bytes + size > MAX_TOTAL_BYTES:
                self._errors.append(
                    f"{path}: workflow directory total exceeds "
                    f"{MAX_TOTAL_BYTES} bytes; skipping remaining files"
                )
                break
            total_bytes += size

            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
            except Exception as e:  # noqa: BLE001
                self._errors.append(f"{path}: parse failed: {e}")
                continue

            if data is None:
                continue

            # A file can contain a single workflow (dict) or multiple
            # (list of dicts).
            entries: list[Any]
            if isinstance(data, dict):
                entries = [data]
            elif isinstance(data, list):
                entries = data
            else:
                self._errors.append(
                    f"{path}: top-level must be a mapping or list, "
                    f"got {type(data).__name__}"
                )
                continue

            for i, entry in enumerate(entries):
                try:
                    wf = WorkflowDef.from_dict(entry, source_path=path)
                    workflows.append(wf)
                except ValueError as e:
                    self._errors.append(f"{path} [{i}]: {e}")

        return workflows
