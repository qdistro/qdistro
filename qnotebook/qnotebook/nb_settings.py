"""Per-notebook settings stored in ``.qnotebook/settings.json``.

Keys:
  - versioning_enabled: bool
  - versioning_prompted: bool  (one-time prompt flag)
  - strict_preserve: bool      (refuse to rewrite regions we couldn't round-trip)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SETTINGS_FILE = ".qnotebook/settings.json"

DEFAULTS: dict[str, Any] = {
    "versioning_enabled": True,
    "versioning_prompted": False,
    "strict_preserve": True,
}


def path_for(root: Path) -> Path:
    return Path(root) / SETTINGS_FILE


def load(root: Path) -> dict[str, Any]:
    p = path_for(root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save(root: Path, data: dict[str, Any]) -> None:
    p = path_for(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Internal state, exempt from SafeWriter (not user content).
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get(root: Path, key: str, default=None) -> Any:
    data = load(root)
    if key in data:
        return data[key]
    return default if default is not None else DEFAULTS.get(key)


def set_value(root: Path, key: str, value: Any) -> None:
    data = load(root)
    data[key] = value
    save(root, data)


def is_new_notebook(root: Path) -> bool:
    """True iff this notebook has no persistent settings yet."""
    return not path_for(root).is_file()
