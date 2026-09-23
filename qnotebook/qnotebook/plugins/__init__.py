"""Plugin discovery and loading.

A plugin is a Python module exposing a `Plugin` class with at least:

    class Plugin:
        name: str
        description: str
        def setup(self, window): ...

Plugins are discovered from two locations:

- bundled: `qnotebook/plugins/builtin/*.py`
- per-notebook: `<notebook>/.qnotebook/plugins/*.py`

Per-notebook plugins are loaded by absolute filesystem path; bundled
plugins are imported as `qnotebook.plugins.builtin.<name>`.

Security — user plugins execute arbitrary code, so trust is *not* keyed on the
filename stem alone (that would let a hostile notebook reuse a stem the user
enabled elsewhere and get auto-executed on open). The persisted "enabled" token
for a user plugin (`enabled_token`) binds (resolved notebook root, filename,
content hash) together, so trust granted to one notebook's `helper.py` never
carries over to a different notebook — or to different content in the same one.
Builtin plugins ship from the trusted bundled dir and stay keyed by stem.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class PluginInfo:
    key: str        # unique identifier (module name or filename stem)
    name: str
    description: str
    source: str     # "builtin" | "user"
    plugin: Any | None     # the Plugin instance; user plugins load only when enabled
    path: Path | None = None
    content_hash: str | None = None  # sha256 of file bytes (user plugins only)


def _instance_from_module(mod) -> Any | None:
    cls = getattr(mod, "Plugin", None)
    if cls is None:
        return None
    try:
        inst = cls()
    except Exception:
        return None
    if not hasattr(inst, "setup"):
        return None
    return inst


def discover(notebook_root: Path | None = None) -> list[PluginInfo]:
    """Find available plugins from builtin + (optional) notebook .qnotebook/plugins."""
    out: list[PluginInfo] = []
    builtin_dir = Path(__file__).parent / "builtin"
    if builtin_dir.is_dir():
        for f in sorted(builtin_dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            modname = f"qnotebook.plugins.builtin.{f.stem}"
            try:
                mod = importlib.import_module(modname)
            except Exception:
                continue
            inst = _instance_from_module(mod)
            if inst is None:
                continue
            out.append(PluginInfo(
                key=f.stem,
                name=getattr(inst, "name", f.stem),
                description=getattr(inst, "description", ""),
                source="builtin",
                plugin=inst,
                path=f,
            ))
    if notebook_root is not None:
        user_dir = notebook_root / ".qnotebook" / "plugins"
        if user_dir.is_dir():
            for f in sorted(user_dir.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                name, description = _read_user_plugin_metadata(f)
                out.append(PluginInfo(
                    key=f"user:{f.stem}",
                    name=name or f.stem,
                    description=description or "",
                    source="user",
                    plugin=None,
                    path=f,
                    content_hash=_hash_file(f),
                ))
    return out


def _hash_file(path: Path) -> str | None:
    """sha256 of the file's raw bytes, or None if it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _user_plugin_trust_id(info: PluginInfo) -> str | None:
    """Content+notebook-bound trust token for a user plugin.

    Binds (resolved notebook root, filename, content hash) so a hostile notebook
    cannot inherit trust granted to a same-named plugin in another notebook, and
    an edit to a trusted plugin re-gates it (fail-closed). Returns None when the
    plugin cannot be located/hashed, in which case it is never auto-executed.
    """
    if info.source != "user" or info.path is None or info.content_hash is None:
        return None
    try:
        # <root>/.qnotebook/plugins/<file>.py -> parents[2] == <root>
        root = info.path.resolve().parents[2]
    except (IndexError, OSError):
        return None
    payload = f"{root}\x00{info.path.name}\x00{info.content_hash}"
    return "userplugin:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enabled_token(info: PluginInfo) -> str | None:
    """The token persisted in settings / matched against the enabled set.

    Builtin plugins (trusted bundled dir) key on their stem; user plugins key on
    a content+notebook-bound trust id. None means "cannot be enabled" (fail-closed).
    """
    if info.source == "user":
        return _user_plugin_trust_id(info)
    return info.key


def _read_user_plugin_metadata(path: Path) -> tuple[str | None, str | None]:
    """Read Plugin.name/description constants without executing plugin code."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None, None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Plugin":
            values: dict[str, str] = {}
            for stmt in node.body:
                target_name = None
                value = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name):
                        target_name = target.id
                    value = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    target_name = stmt.target.id
                    value = stmt.value
                if target_name in ("name", "description"):
                    try:
                        const = ast.literal_eval(value)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(const, str):
                        values[target_name] = const
            return values.get("name"), values.get("description")
    return None, None


def _load_user_plugin(path: Path, expected_hash: str | None) -> Any | None:
    # Read the bytes exactly once and execute THOSE bytes — never re-read through
    # importlib's path loader. This closes the discover->exec TOCTOU: the bytes we
    # run are the bytes we hash here, so a file (or symlink target) swapped after
    # discovery cannot ride the previously-trusted token.
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
        # On-disk content changed since it was trusted → fail closed.
        return None
    # Collision-free module name from the resolved path (never the user-controlled
    # key/stem, which two notebooks can share).
    try:
        ident = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    except OSError:
        ident = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    modname = f"qnotebook_user_plugin_{ident}"
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(modname, loader=None)
    )
    mod.__file__ = str(path)
    try:
        code = compile(data, str(path), "exec")
        exec(code, mod.__dict__)
    except Exception:
        return None
    return _instance_from_module(mod)


def setup_enabled(window, infos: list[PluginInfo], enabled_tokens: set[str]) -> list[str]:
    """Call `setup(window)` on each enabled plugin. Returns list of activated keys.

    `enabled_tokens` holds `enabled_token(info)` values (stem for builtin,
    content+notebook-bound trust id for user plugins) — NOT bare `info.key`. A
    user plugin whose trust token is absent is never loaded, so a hostile
    notebook cannot ride a stem the user trusted elsewhere.
    """
    activated: list[str] = []
    for info in infos:
        token = enabled_token(info)
        if token is None or token not in enabled_tokens:
            continue
        plugin = info.plugin
        if plugin is None and info.source == "user" and info.path is not None:
            plugin = _load_user_plugin(info.path, info.content_hash)
        if plugin is None:
            continue
        try:
            plugin.setup(window)
            activated.append(info.key)
        except Exception:
            pass
    return activated
