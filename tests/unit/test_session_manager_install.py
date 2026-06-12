"""Static guard: the session-manager installer ships every sibling module
the daemon imports.

`qdistro_session_manager.py` runs from /usr/libexec/qdistro/ with that dir on
sys.path[0], so it imports its `qdistro_*` siblings by bare module name. Each
such sibling MUST be copied into $DEST by install-session-manager.sh, or the
daemon ModuleNotFoundErrors and crash-loops on boot (regression: M3 added
`import qdistro_disposables` but not the matching install line, which wedged
every VM gate that needs the session up).

This is a pure host test — it reads the two source files and compares the
import set against the install set. No VM, no root.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SM_DIR = _REPO / "session_manager"
_DAEMON = _SM_DIR / "qdistro_session_manager.py"
_INSTALLER = _REPO / "scripts" / "install" / "install-session-manager.sh"

# `install ... "$SRC/qdistro_foo.py" ...`
_INSTALL_RE = re.compile(r"\$SRC/(qdistro_[A-Za-z0-9_]+)\.py")


def _qdistro_imports(path: Path) -> set[str]:
    """qdistro_* modules imported by `path`, parsed as Python."""
    imports: set[str] = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".", 1)[0]
                if name.startswith("qdistro_"):
                    imports.add(name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            name = node.module.split(".", 1)[0]
            if name.startswith("qdistro_"):
                imports.add(name)
    return imports


def _imported_siblings() -> set[str]:
    """qdistro_* sibling modules in the daemon import closure."""
    pending = ["qdistro_session_manager"]
    seen: set[str] = set()
    imported: set[str] = set()

    while pending:
        mod = pending.pop()
        if mod in seen:
            continue
        seen.add(mod)

        path = _SM_DIR / f"{mod}.py"
        if not path.is_file():
            continue

        for dep in _qdistro_imports(path):
            # Only siblings shipped from session_manager/ are the installer's
            # job; a qdistro_* import resolved from site-packages is out of scope.
            if (_SM_DIR / f"{dep}.py").is_file():
                imported.add(dep)
                pending.append(dep)

    return imported


def _installed_modules() -> set[str]:
    return set(_INSTALL_RE.findall(_INSTALLER.read_text()))


def test_installer_ships_every_imported_sibling() -> None:
    imported = _imported_siblings()
    installed = _installed_modules()
    missing = imported - installed
    assert not missing, (
        "install-session-manager.sh does not install these sibling modules "
        f"imported by qdistro_session_manager.py: {sorted(missing)}. "
        "Add an `install -m 0755 \"$SRC/<mod>.py\" \"$DEST/<mod>.py\"` line "
        "or the daemon will crash-loop with ModuleNotFoundError on boot."
    )


def test_disposables_is_installed() -> None:
    # Explicit anchor for the M3 regression this test was written for.
    assert "qdistro_disposables" in _installed_modules()
