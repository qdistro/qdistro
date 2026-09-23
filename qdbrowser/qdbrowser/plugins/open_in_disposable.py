"""Open-in-disposable plugin for qdbrowser.

Adds a *Preview this page in a disposable* command (command palette, Ctrl+E)
that routes the current tab's URL to the qdistro disposable surface — it opens
the URL inside a fresh, throwaway tier-2 ``url-preview`` container (a minimal,
sandboxed metadata/text preview, NOT a browser) so an untrusted link can be
inspected without loading it in the trusted browser session.

The plugin is a thin GUI consumer of the SHIPPED SDK surface
``qdistro_app.open_in_disposable(path, class_name=…)``. That SDK helper, and
the trusted launch binary behind it, are the security boundary: they resolve
the class from the disposable-class registry (enforcing the ``min_tier`` gate),
call the broker ``qdistro.dispose.open:<class>`` gate (rules-only / fail-closed)
and the silo-egress network contract, and bind the input read-only. This plugin
NEVER re-implements any of that — it only:

* decides a URL is ELIGIBLE for a preview disposable — an ``http(s)`` URL with a
  host — and maps it to a FIXED class-name string literal (so a URL can never
  inject an arbitrary class name into the broker action), and
* hides the command when there is no eligible URL, when the SDK isn't
  importable, or when a cheap, bounded probe of the shipped class resolver says
  the preview class is disabled. The probe is UI hygiene only — the SDK call on
  click is the real authority and surfaces any refusal as a notification.

The URL is handed to the disposable the only way the trusted contract accepts an
input: as a read-only file. The plugin writes the URL to a per-call temp file
(in the user-private runtime dir) and passes that path to the SDK; the
``url-preview`` workload reads it from its read-only ``/mnt/input`` bind.

Fail-closed throughout: a missing SDK, a missing/erroring resolver, a probe
timeout, a disabled class, or an ineligible URL all yield "no command" (or a
clear error on click), never a silent wrong-thing.

:func:`resolve_url_class` and :func:`class_enabled` are pure-ish helpers (no Qt,
deterministic given their inputs) so the routing logic is unit-testable without
launching anything.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from qdbrowser.plugin import CommandProvider

log = logging.getLogger(__name__)

# The registry class a URL previews in: a minimal, sandboxed, egress-gated
# metadata/text preview of a single known-origin URL (NOT a browser). This is
# the ONLY URL-opening disposable class the tier-2 default enables; everything
# that is not a plain http(s) URL is intentionally left UNMAPPED so a hostile
# scheme (file:, data:, javascript:, about:, …) can never be routed to a
# disposable from the browser.
URL_PREVIEW_CLASS = "url-preview-known-origin"

# Schemes eligible for a preview disposable. Deliberately just the two web
# schemes — file:/data:/javascript:/about:/blob:/ftp: are NEVER previewed.
_ELIGIBLE_SCHEMES = frozenset({"http", "https"})

# How long the advisory enablement probe may run before failing closed. The
# resolver is a tiny pure-Python CLI; a slow / wedged one must never block the
# command palette.
_PROBE_TIMEOUT_S = 5

# The shipped class-registry resolver. Same default path the SDK uses; an env
# override (honoured by the SDK too) lets tests point at the in-tree copy.
_RESOLVER_DEFAULT = "/usr/libexec/qdistro/qdistro_disposable_classes.py"


def _sdk():
    """Return the ``qdistro_app`` SDK module, or ``None`` if unavailable.

    Lazy + swallowing: an import-time failure (SDK not installed, broken dbus,
    …) must not break plugin discovery or the rest of the command palette.
    """
    try:
        import qdistro_app
        return qdistro_app
    except Exception as e:  # noqa: BLE001 - any import failure means "no SDK"
        log.debug("qdistro_app SDK unavailable: %s", e)
        return None


def resolve_url_class(url: str) -> str | None:
    """Resolve the preview disposable class for ``url``, or ``None``.

    Returns a class name from a FIXED literal — the class is NEVER derived from
    the URL's bytes, so a hostile URL can only ever resolve to the one
    known-safe literal or ``None``. Eligible: a well-formed ``http``/``https``
    URL with a non-empty host. Everything else (other schemes, no host, empty,
    non-str) → ``None``.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in _ELIGIBLE_SCHEMES:
        return None
    if not parts.hostname:
        return None
    return URL_PREVIEW_CLASS


def _resolver_path() -> str:
    return (os.environ.get("QDISTRO_DISPOSABLE_CLASSES_RESOLVER")
            or _RESOLVER_DEFAULT)


def class_enabled(class_name: str, *, _cache: dict | None = None) -> bool:
    """Advisory, fail-closed probe: is ``class_name`` enabled in the shipped
    registry?  Runs the resolver CLI (the same authority the SDK + trusted
    binary use) with a bounded timeout. Any non-zero exit, timeout, missing
    resolver, or OS error → ``False`` (hide the command). Result cached per
    (resolver, class) for the process lifetime — UI hygiene, NOT a security
    boundary (the SDK call on click re-checks for real).
    """
    cache = _ENABLED_CACHE if _cache is None else _cache
    resolver = _resolver_path()
    key = (resolver, class_name)
    if key in cache:
        return cache[key]
    enabled = False
    try:
        # Mirror the SDK / qfileman exactly: invoke the resolver WITHOUT an
        # explicit --registry so it uses the same production-default registry
        # (and honours the same env override) the trusted spawn path uses — no
        # drift between this advisory probe and the real authority on click.
        proc = subprocess.run(
            ["python3", resolver, "--resolve", class_name],
            capture_output=True, timeout=_PROBE_TIMEOUT_S, check=False)
        enabled = proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("class-enabled probe for %s failed: %s", class_name, e)
        enabled = False
    cache[key] = enabled
    return enabled


_ENABLED_CACHE: dict = {}


class OpenInDisposablePlugin(CommandProvider):
    """Command-palette entry that previews the current URL in a disposable."""

    name = "open_in_disposable"
    description = "Preview an untrusted URL in a throwaway tier-2 disposable"
    capabilities = ["command_provider"]

    def __init__(self):
        super().__init__()
        self._window = None
        self._staged: list[str] = []  # temp dirs to clean on deactivate

    def activate(self, window):
        self._window = window

    def deactivate(self):
        # Best-effort cleanup of staged URL files (each is RO-bound into a live
        # disposable while it runs; we only created small files in the private
        # runtime dir, so an undeleted one is harmless — clean what we can).
        for d in self._staged:
            shutil.rmtree(d, ignore_errors=True)
        self._staged.clear()

    def get_commands(self, window):
        wv = getattr(window, "_active_webview", None)
        url = ""
        if wv is not None:
            try:
                url = wv.url() or ""
            except Exception:  # noqa: BLE001 - a webview hiccup must not break the palette
                url = ""
        cls = resolve_url_class(url)
        if cls is None:
            return []
        if _sdk() is None:
            return []
        if not class_enabled(cls):
            return []
        return [("Preview this page in a disposable",
                 lambda u=url, c=cls: self._preview(u, c))]

    # -- click handler -----------------------------------------------------
    def _preview(self, url: str, class_name: str) -> None:
        sdk = _sdk()
        if sdk is None:  # re-check; SDK could have gone away since menu build
            self._notify("Open in disposable: the qdistro SDK is unavailable.")
            return
        try:
            path = self._stage_url(url)
        except OSError as e:
            self._notify(f"Open in disposable: could not stage the URL ({e}).")
            return
        try:
            sdk.open_in_disposable(path, class_name=class_name)
        except Exception as e:  # noqa: BLE001 - surface ANY SDK refusal as a notice
            log.info("open_in_disposable(%r, %s) refused: %s", url, class_name, e)
            self._notify(f"Open in disposable refused: {e}")

    # -- helpers -----------------------------------------------------------
    def _stage_url(self, url: str) -> str:
        """Write ``url`` to a fresh read-only-bindable file and return its path.

        The file lives in the user-private runtime dir (tmpfs, wiped on logout).
        The trusted binary binds it read-only at ``/mnt/input/url`` in the
        disposable; the ``url-preview`` workload reads the URL from there.
        """
        # Stage under a FRESH, unique 0700 directory created ATOMICALLY by
        # mkdtemp — no predictable shared parent for a same-host attacker to
        # pre-plant or symlink-redirect. Prefer the user-private runtime dir
        # ($XDG_RUNTIME_DIR, /run/user/<uid> tmpfs); when it is unset, mkdtemp's
        # own O_EXCL dir creation in the system temp is still not
        # cross-user-plantable (random name, fails if it exists).
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        d = tempfile.mkdtemp(
            prefix="qdbrowser-disp-url-",
            dir=runtime if runtime and os.path.isdir(runtime) else None)
        self._staged.append(d)
        path = os.path.join(d, "url")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(url.strip() + "\n")
        os.chmod(path, 0o600)
        return path

    def _notify(self, message: str) -> None:
        """Surface a message to the user, swallowing any GUI import/runtime
        error (a notification must never crash the browser)."""
        log.info("%s", message)
        win = self._window
        try:
            notify = getattr(win, "notify", None)
            if callable(notify):
                notify(message)
                return
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(win, "Open in disposable", message)
        except Exception as e:  # noqa: BLE001
            log.debug("could not surface notification %r: %s", message, e)
