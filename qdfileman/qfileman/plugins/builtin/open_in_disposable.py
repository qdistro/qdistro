"""Open-in-disposable plugin for QFileMan.

Adds *Open in Disposable* to the file context menu. The action routes the
right-clicked file to the qdistro disposable surface — it opens the file
READ-ONLY inside a fresh, throwaway tier-2 container so an untrusted file
can be viewed without exposing the user's silo to it.

The plugin is a thin GUI consumer of the SHIPPED SDK surface
``qdistro_app.open_in_disposable(path, class_name=…)``. That SDK helper, and
the trusted launch binary behind it, are the security boundary: they resolve
the class from the disposable-class registry (enforcing the ``min_tier``
hostile-class gate), call the broker ``qdistro.dispose.open:<class>`` gate
(rules-only / fail-closed), and bind the input read-only. This plugin NEVER
re-implements any of that — it only:

* resolves a SENSIBLE class for the file from its MIME type / extension, from a
  FIXED allowlist of class-name string literals (so a filename can never inject
  an arbitrary class name into the broker action), and
* hides the menu item when there is no enabled class for the file's type, when
  the SDK isn't importable, or when a cheap, bounded probe of the shipped class
  resolver says the class is disabled. The probe is UI hygiene only — the SDK
  call on click is the real authority and surfaces any refusal as a dialog.

Fail-closed throughout: a missing SDK, a missing/erroring resolver, a probe
timeout, or a disabled class all yield "no item" (or a clear error on click),
never a silent wrong-thing.

:func:`resolve_class_for_path` and :func:`class_enabled` are pure-ish helpers
(no Qt, deterministic given their inputs) so the routing logic is unit-testable
without launching anything.

Only ``open_in_disposable`` is wired. The edit-round-trip
(``open_for_edit`` / ``notify_edit_ready``) and import-back
(``ImportFromDisposable``) surfaces are NOT yet present in the SDK; they are
left as residual milestone items rather than stubbed here — a plugin must not
invent SDK surfaces or simulate the trusted disposable contract.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# The registry class a text file opens in. This is the ONLY enabled
# file-opening disposable class at the tier-2 default today (the registry also
# enables ``agent-scratch`` — a throwaway shell, not a file opener — and
# ``url-preview-known-origin`` — for URLs, not files). The hostile-input
# classes ``pdf`` / ``office`` / ``archive`` are deliberately DISABLED in the
# registry (min_tier 4) and intentionally left UNMAPPED here, so a disguised
# document can never be routed to a tier-2 disposable from the file manager.
TEXT_CLASS = "text/plain"

# Extensions we treat as plain text even when ``mimetypes`` doesn't (or maps
# them to a non-text/* type like application/json). Conservative on purpose:
# every entry here opens in the read-only, no-network text-viewer class.
_TEXT_EXTENSIONS = frozenset({
    ".txt", ".text", ".md", ".markdown", ".rst", ".log",
    ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".toml",
    ".ini", ".conf", ".cfg", ".properties",
    ".py", ".sh", ".bash", ".zsh", ".pl", ".rb", ".lua",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".rs", ".go", ".java",
    ".js", ".ts", ".css", ".sql", ".diff", ".patch",
})

# How long the advisory enablement probe may run before we fail closed. The
# resolver is a tiny pure-Python CLI; a slow / wedged one must never block the
# context menu from appearing.
_PROBE_TIMEOUT_S = 5

# The shipped class-registry resolver. Same default path the SDK uses; env
# overrides (honoured by the SDK too) let tests point at the in-tree copy.
_RESOLVER_DEFAULT = "/usr/libexec/qdistro/qdistro_disposable_classes.py"


def _sdk():
    """Return the ``qdistro_app`` SDK module, or ``None`` if unavailable.

    Lazy + swallowing: an import-time failure (SDK not installed, broken
    dbus, …) must not break plugin discovery or the rest of the menu.
    """
    try:
        import qdistro_app
        return qdistro_app
    except Exception as e:  # noqa: BLE001 - any import failure means "no SDK"
        log.debug("qdistro_app SDK unavailable: %s", e)
        return None


def resolve_class_for_path(path: str) -> str | None:
    """Resolve the disposable class for ``path`` from its type, or ``None``.

    Returns a class name from a FIXED allowlist of literals — the class is NEVER
    derived from the filename's bytes, so a hostile name (``evil; rm -rf.pdf``)
    can only ever resolve to one of the known-safe literals or ``None``. Today
    that means: plain-text files → :data:`TEXT_CLASS`; everything else (binaries,
    images, pdf/office/archive, unknown) → ``None``. Directories return ``None``
    (the enabled file classes are file viewers; ``agent-scratch`` is a shell,
    not "open this file").
    """
    if not path or not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in _TEXT_EXTENSIONS:
        return TEXT_CLASS
    mime, _enc = mimetypes.guess_type(path)
    if mime and mime.startswith("text/"):
        return TEXT_CLASS
    return None


def _resolver_path() -> str:
    return (os.environ.get("QDISTRO_DISPOSABLE_CLASSES_RESOLVER")
            or _RESOLVER_DEFAULT)


# Process-lifetime cache of the advisory enablement probe, keyed by
# (resolver_path, class_name). ``get_menu_items`` runs synchronously on the Qt
# UI thread for EVERY right-click (pane.py), so the probe must not re-shell a
# fresh python3 + TOML parse each time — that would freeze the context menu (up
# to the timeout) on every click. The registry is static config for a session,
# so probing once per class and caching is both correct and keeps the menu
# responsive. Tests reset it via :func:`_reset_enablement_cache`.
_enabled_cache: dict[tuple[str, str], bool] = {}


def _reset_enablement_cache() -> None:
    """Clear the probe cache (test seam; also a future 'registry changed' hook)."""
    _enabled_cache.clear()


def _probe_class_enabled(resolver: str, class_name: str) -> bool:
    """Shell the SHIPPED resolver once with an argv list (no shell), a short
    timeout, the registry env honoured. Exit 0 → enabled; ANY other outcome —
    non-zero (unknown/disabled/malformed), missing resolver, timeout, OSError —
    → ``False`` (fail-closed)."""
    if not os.path.exists(resolver):
        log.debug("class resolver not installed: %s", resolver)
        return False
    try:
        proc = subprocess.run(
            ["python3", resolver, "--resolve", class_name],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # subprocess.TimeoutExpired is a SubprocessError — covered here.
        log.debug("class-enabled probe failed for %r: %s", class_name, e)
        return False
    return proc.returncode == 0


def class_enabled(class_name: str) -> bool:
    """Best-effort, FAIL-CLOSED, CACHED probe of whether ``class_name`` is
    enabled in the shipped disposable-class registry.

    The result is cached for the process lifetime (keyed by resolver path +
    class) so a right-click never pays a fresh subprocess on the Qt UI thread —
    only the first probe of a given class shells out. This is UI hygiene ONLY:
    it greys an item we already know the daemon would refuse. The real authority
    is :func:`OpenInDisposablePlugin._open`'s ``open_in_disposable`` call, which
    re-runs every gate; so a cache that's momentarily stale can at worst show an
    item that then errors cleanly on click — never open something un-isolated.
    """
    key = (_resolver_path(), class_name)
    cached = _enabled_cache.get(key)
    if cached is None:
        cached = _probe_class_enabled(*key)
        _enabled_cache[key] = cached
    return cached


class OpenInDisposablePlugin(MenuProvider):
    name = "open_in_disposable"
    description = "Open a file read-only inside a throwaway disposable container"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        # Fail-closed at every step: any miss yields no item rather than a
        # broken / misleading action.
        if not path or _sdk() is None:
            return []
        cls = resolve_class_for_path(path)
        if cls is None:
            return []
        if not class_enabled(cls):
            return []
        return [("Open in Disposable", self._open)]

    def _open(self, path: str) -> None:
        """Route ``path`` to the disposable surface, re-resolving on click.

        Nothing is cached from menu construction — the registry/file/SDK can
        change between right-click and click, so we re-resolve the class and
        re-check the file here, and let the SDK call be the source of truth.
        Any refusal / failure surfaces as a dialog; we never silently no-op
        into the wrong thing.
        """
        sdk = _sdk()
        if sdk is None:
            self._warn("qdistro disposable SDK is not available.")
            return
        cls = resolve_class_for_path(path)
        if cls is None:
            self._warn("This file has no enabled disposable class.")
            return
        # The SDK requires an absolute path (and realpath-normalizes itself);
        # the pane usually hands us one, but normalize defensively so a relative
        # cwd doesn't turn into a needless refusal dialog.
        abspath = os.path.abspath(path)
        try:
            sdk.open_in_disposable(abspath, class_name=cls)
        except Exception as e:  # noqa: BLE001 - surface every failure to the user
            log.warning("open_in_disposable(%s, class=%s) failed: %s",
                        path, cls, e)
            self._warn(f"Could not open in disposable: {e}")

    @staticmethod
    def _warn(message: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        log.warning(message)
        QMessageBox.warning(None, "Open in Disposable", message)
