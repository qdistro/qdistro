"""Classify special files that qnotebook should open read-only or warn on.

Matchers:
  - *.excalidraw.md                  -> read-only (Excalidraw drawing)
  - frontmatter has excalidraw-plugin: -> read-only
  - frontmatter has kanban-plugin:   -> editable but warn
  - file size > 500 KB               -> warn before loading
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LARGE_FILE_BYTES = 500 * 1024


@dataclass(frozen=True)
class FileMode:
    readonly: bool
    warn: bool
    reason: str


def _has_frontmatter_key(data: bytes, key: str) -> bool:
    text = data.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    block = text[3:end]
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith(key + ":"):
            return True
    return False


def classify(path: Path, size: int | None = None,
             data: bytes | None = None) -> FileMode:
    path = Path(path)
    name = path.name
    sz = size if size is not None else (path.stat().st_size if path.is_file() else 0)

    if name.endswith(".excalidraw.md"):
        return FileMode(True, True, "Excalidraw drawing — edit in Excalidraw app")

    if data is None and path.is_file():
        try:
            data = path.read_bytes()[:4096]
        except OSError:
            data = b""
    data = data or b""

    if _has_frontmatter_key(data, "excalidraw-plugin"):
        return FileMode(True, True, "Excalidraw drawing — edit in Excalidraw app")

    if _has_frontmatter_key(data, "kanban-plugin"):
        return FileMode(False, True,
                        "Kanban board — structural edits may break the plugin")

    if sz > LARGE_FILE_BYTES:
        return FileMode(True, True,
                        f"Large file ({sz // 1024} KB) — opened read-only")

    return FileMode(False, False, "")
