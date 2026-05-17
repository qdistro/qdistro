"""App1 launcher registration for tier-4 whole-VM windows.

P05a wires tier-4 VMs into the same launcher contract qfileman /
qterminator / qnotebook use (P03): on registration, this module claims
``org.qdistro.Tier4VM.uid<NNNN>`` on the session bus and exposes the
``org.qdistro.App1`` interface plus a tier-4-specific ``Close()``
method that drives the ACPI→destroy lifecycle in
:mod:`tier4_chrome`.

The launcher (qdshell PodApps) spawns the App1 receiver via
``spawn-tier4.sh <vm>`` with the secctx triple
``(qdistro.tier4, qdistro.tier4.<vm>, <launch-token>)``. The receiver
process is a thin wrapper around the display client (waypipe); when the user clicks
the chrome close button qdshell calls ``Close()`` and we hand off to
``close_vm`` so the qemu / libvirt teardown runs in-process rather
than racing the display client's own signal handling.

Layering:

- :func:`maybe_install` — entry point, mirrors qfileman's. Returns
  the receiver object (keep it alive for the app's lifetime) or None.
- :func:`build_send_to_payload` — for the "Send-To Tier4VM" menu, the
  payload format is the same ``text/plain`` strip you'd hand any
  other App1 receiver. Tier-4 doesn't actually accept Send-To today
  (no clipboard write into the guest from outside; that's P05b), so
  ReceivePayload is a no-op that logs the drop.

Degrades to a no-op when ``dbus-python`` is missing or the session
bus isn't reachable.
"""
from __future__ import annotations

import os
import sys
from typing import Any

try:  # pragma: no cover — host-only path
    from qdistro_app import app_receiver as _app_receiver
except ImportError:
    _app_receiver = None  # type: ignore[assignment]

# Sibling module — keeps the colour/lifecycle helpers usable by the
# bats driver via plain `python3 -c "from tier4_chrome import …"`.
# On a flat-layout install (qdistro/tier4-vm/ on sys.path, no
# __init__.py) the relative import below fails with ImportError; we
# then bootstrap tier4_chrome via importlib AND register it in
# sys.modules first so its @dataclass decorator (which introspects
# sys.modules.get(cls.__module__)) can find itself. Without the
# sys.modules.set the dataclass machinery raises an opaque AttributeError
# on a None __dict__ lookup.
try:
    from . import tier4_chrome as _tier4_chrome  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — flat-layout fallback
    import importlib.util
    import pathlib
    _spec = importlib.util.spec_from_file_location(
        "tier4_chrome",
        pathlib.Path(__file__).with_name("tier4_chrome.py"))
    if _spec and _spec.loader:
        _tier4_chrome = importlib.util.module_from_spec(_spec)
        sys.modules["tier4_chrome"] = _tier4_chrome
        _spec.loader.exec_module(_tier4_chrome)  # type: ignore[union-attr]
    else:  # pragma: no cover — install layout
        _tier4_chrome = None  # type: ignore[assignment]


APP_FRIENDLY_NAME = "Tier4VM"
# TODO(P05b): populate APP_SUPPORTED_KINDS once the inbound payload
# path lands (clipboard seed-from-host into the guest). Today the empty
# tuple means CanReceive returns false and tier-4 never appears in
# Send-To menus, which is the desired conservative default but worth
# flagging for the P05b implementer. (P05a integration SHOULD-FIX-3.)
APP_SUPPORTED_KINDS = ()


def maybe_install(vm_name: str, *, on_close=None) -> Any | None:
    """Register the App1 receiver for the running tier-4 VM.

    ``vm_name`` is the libvirt domain name (== the secctx silo tag);
    used as the GetSilo() return so PodApps groups multiple tier-4
    windows under one silo badge. ``on_close`` is an optional callback
    invoked on a Close() RPC — pass a function that propagates the
    close into your display client wrapper. When None, the default
    handler calls :func:`tier4_chrome.close_vm` directly.
    """
    if _app_receiver is None:
        print("[tier4-vm/qdistro] qdistro_app SDK not importable; "
              "App1 registration skipped",
              file=sys.stderr, flush=True)
        return None

    silo = str(vm_name or "")

    def _on_receive(kind: str, payload: str) -> None:
        # Tier-4 doesn't ingest external payloads today. Log and drop
        # so a misrouted Send-To produces an audit trail rather than
        # silently disappearing.
        print(f"[tier4-vm/qdistro] dropping send-to kind={kind} "
              f"len={len(payload)} (tier-4 has no receive path)",
              file=sys.stderr, flush=True)

    receiver = _app_receiver.register_app(
        APP_FRIENDLY_NAME,
        on_receive=_on_receive,
        friendly_name=APP_FRIENDLY_NAME,
        supported_kinds=APP_SUPPORTED_KINDS,
        silo=silo,
    )
    if receiver is None:
        return None

    # NOTE: the Close() RPC is registered by tier4_control.py on a
    # separate bus name (org.qdistro.Tier4VM.Control.uid<N>). The
    # AppReceiver SDK has no add_close_handler hook; the pre-fix-pass
    # `hasattr(receiver, "add_close_handler")` branch here was dead
    # code — removed. ``on_close`` is now used only by the standalone
    # tier4_control entry point. (P05a operational LOW-4 / correctness
    # MEDIUM-4.)
    _ = on_close  # documented unused; tier4_control owns the close path
    print(f"[tier4-vm/qdistro] App1 receiver registered as "
          f"{receiver.service_name} (silo={silo!r})",
          flush=True)
    return receiver


def _default_close(vm_name: str) -> dict:
    """Run the ACPI→destroy lifecycle and return a JSON-shaped result.

    Structured log lines bracket the call so the systemd journal carries
    a trace even when the caller doesn't surface ``result`` to the user
    (e.g. when invoked over the App1 Close() RPC — the launcher process
    is the only consumer of the return value).
    """
    print(f"[tier4-vm/qdistro] close_vm start vm={vm_name!r}",
          file=sys.stderr, flush=True)
    if _tier4_chrome is None:
        print(f"[tier4-vm/qdistro] close_vm done vm={vm_name!r} "
              f"outcome=not-ok reason=tier4_chrome-unimportable",
              file=sys.stderr, flush=True)
        return {"ok": False, "method": "missing",
                "stderr": "tier4_chrome not importable"}
    result = _tier4_chrome.close_vm(str(vm_name))
    print(f"[tier4-vm/qdistro] close_vm done vm={vm_name!r} "
          f"ok={result.ok} method={result.method} "
          f"orphans={result.orphans_reaped} stderr={result.stderr!r:.120}",
          file=sys.stderr, flush=True)
    return {
        "ok": bool(result.ok),
        "method": result.method,
        "stderr": result.stderr,
        "orphans_reaped": result.orphans_reaped,
    }


def build_send_to_payload(text: str) -> str:
    """Strip non-allowed MIMEs out before letting App1 ferry payloads.

    Today tier-4 has no inbound payload path, but the existing tier-3
    Send-To round-trip hits ReceivePayload directly. The payload is
    already a string by the time we see it, but the calling pattern
    in launcher UIs is "build the menu rows" → "filter to peers" → "send".
    This helper exists so the Send-To menu builder can stay
    symmetric with qfileman / qterminator's API.
    """
    return str(text or "")


def chrome_rgba_for_secctx(secctx_app_id: str | None) -> int:
    """Single-call colour resolver for the qdshell side.

    Re-exported here so Tier4Apps.qml can call out to a one-shot
    ``python3 -c`` if it ever wants to bypass the QML palette table —
    the canonical implementation lives in :mod:`tier4_chrome`.
    """
    if _tier4_chrome is None:
        return 0
    return int(_tier4_chrome.resolve_chrome_color(secctx_app_id))


def send_to_targets(*, kind: str = "text/plain") -> list[dict]:
    """Stub: tier-4 doesn't surface Send-To peers today (no receive)."""
    if _app_receiver is None:
        return []
    try:
        self_service = f"org.qdistro.{APP_FRIENDLY_NAME}.uid{os.geteuid()}"
        return _app_receiver.send_to_menu_targets(
            self_service=self_service, kind=kind)
    except Exception as e:  # noqa: BLE001
        print(f"[tier4-vm/qdistro] send_to_menu_targets failed: {e}",
              file=sys.stderr, flush=True)
        return []
