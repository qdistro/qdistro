"""Tier-4 VM chrome — silo-colour resolver + close-button lifecycle.

This module exists so the tier-4 VM window chrome (server-side
decoration painted by qdshell, colour negotiated via qdwin_shell_v1's
``set_border_color`` on a per-toplevel handle) reads from **the same
secctx tag source** that tier-3 uses for podman silo badges. Single
source of truth: ``secctx_app_id`` ⇒ silo ⇒ colour.

The two public entry points:

- :func:`resolve_chrome_color` — given a secctx app_id, return the
  packed RGBA byte string the SSD paint helper expects. Pure function;
  no dbus, no compositor, fully unit-testable.
- :func:`close_vm` — ACPI-soft-shutdown a tier-4 libvirt domain with a
  5-second timeout, then ``virsh destroy`` if the guest is still alive,
  and reap any orphan ``qdistro-forward`` helpers. Returns a structured
  result the close-button caller logs.

P05a wires these via:

- ``qdistro_integration.py`` (App1 launcher entry) — claims
  ``com.qdistro.Tier4VM.uid<NNNN>`` and exposes a "Close" method.
- ``Tier4Apps.qml`` (qdshell) — calls ``QdwinBinding.setBorderColor``
  with the resolved rgba on every ``toplevelSecurityContext`` event.

Per task P05a: the chrome-colour source is ``secctx_tag.color`` — i.e.
derive deterministically from the secctx tag the launcher injected so a
malicious guest can't repaint its own chrome. (The compositor reads
the tag from ``wp_security_context_v1``, not from the guest.)
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence


# Domain-name shape shared with spawn-tier4.sh:90 — single source of truth
# so both spawn and close enforce the same allow-list. Reject leading
# dashes / whitespace / regex metachars / option-injection shapes.
_VM_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


def _vm_name_is_safe(vm_name: str | None) -> bool:
    """Conservative vm_name allow-list. Rejects:
      - empty / None
      - leading dash (virsh option-injection)
      - slash / NUL (path traversal)
      - whitespace (trailing space pads → silent virsh failure)
      - regex metachars (pgrep -f interpolation defence)
    """
    if not vm_name:
        return False
    s = str(vm_name)
    if not _VM_NAME_RE.match(s):
        return False
    return True


# ---- silo-colour palette ----

# 10 visible hex colours that survive both light and dark themes —
# same palette qdshell's Tier3Apps.qml carries. Repeating it here lets
# the qdistro-side launcher pre-compute the rgba qdshell will apply,
# and lets us unit-test the resolution end-to-end without a running
# QML engine.
SILO_PALETTE_HEX: tuple[str, ...] = (
    "#4caf50",  # green
    "#ffb300",  # amber/yellow
    "#2196f3",  # blue
    "#ab47bc",  # magenta/purple
    "#26c6da",  # cyan
    "#8bc34a",  # bright green
    "#ffe54c",  # bright yellow
    "#64b5f6",  # bright blue
    "#ce93d8",  # bright magenta
    "#80deea",  # bright cyan
)


# Tier-4 secctx app_ids carry the ``qdistro.tier4.`` prefix; the silo
# tag follows. spawn-tier4.sh wraps the display client via qdistro-secctx-exec
# with --app-id qdistro.tier4.<vm_name>, so the silo == the vm name
# unless the launcher overrides it with TIER4_SECCTX_APPID.
TIER4_SECCTX_PREFIX = "qdistro.tier4."


def silo_from_secctx(secctx_app_id: str | None) -> str:
    """Return the silo label for a tier-4 secctx app_id.

    Returns ``""`` when the app_id is missing or doesn't carry the
    tier-4 prefix — callers fall back to a neutral chrome rather than
    spoof-paint with a wrong colour.
    """
    if not secctx_app_id:
        return ""
    if not secctx_app_id.startswith(TIER4_SECCTX_PREFIX):
        return ""
    return secctx_app_id[len(TIER4_SECCTX_PREFIX):]


def _hash_index(silo: str) -> int:
    """Deterministic char-sum hash → palette index.

    Mirrors qdshell/Services/Qdistro/Tier3Apps.qml::colourForSilo so a
    given silo always paints with the same colour across tier-3 and
    tier-4. Implemented as a plain summation because (a) it's stable
    across Python/JS, (b) collisions inside a 10-bucket palette are
    fine — the goal is "skim-distinguishable", not "cryptographic".
    """
    h = 0
    for ch in silo:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h % len(SILO_PALETTE_HEX)


def silo_color_hex(silo: str) -> str:
    """Return ``#rrggbb`` for a silo, or the first palette entry on empty input."""
    if not silo:
        return SILO_PALETTE_HEX[0]
    return SILO_PALETTE_HEX[_hash_index(silo)]


def hex_to_rgba(hex_color: str, alpha: int = 0xFF) -> int:
    """Pack ``#rrggbb`` into the 0xRRGGBBAA uint32 ``set_border_color`` wants.

    ``set_border_color`` documents rgba as big-endian RGBA8888 (the
    natural Wayland convention): the high byte is R, low byte is A.
    A bad input raises ValueError early so the launcher fails loud
    rather than painting transparent black.
    """
    s = (hex_color or "").lstrip("#").strip()
    if len(s) != 6:
        raise ValueError(f"hex_to_rgba: expected #rrggbb, got {hex_color!r}")
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except ValueError as e:
        raise ValueError(f"hex_to_rgba: non-hex digits in {hex_color!r}") from e
    a = int(alpha) & 0xFF
    return (r << 24) | (g << 16) | (b << 8) | a


def resolve_chrome_color(secctx_app_id: str | None) -> int:
    """Single-call resolver: secctx_app_id → packed RGBA for set_border_color.

    Returns 0 when no tier-4 silo could be derived. qdshell's chrome
    painter treats 0 as "fall back to the neutral default" — the
    contract documented in qdwin_toplevel_border_rgba().
    """
    silo = silo_from_secctx(secctx_app_id)
    if not silo:
        return 0
    return hex_to_rgba(silo_color_hex(silo))


# ---- close-button lifecycle ----

@dataclass
class CloseResult:
    """What the close-button caller logs.

    ``ok`` is True when the guest is gone *and* no orphans remain.
    ``method`` ∈ ``"acpi"`` (clean ACPI shutdown won the race) /
    ``"destroy"`` (timeout → hard destroy fired) / ``"missing"``
    (the domain was already gone). ``stderr`` carries any virsh error
    output so the caller can pass it to the user / journal.
    """
    ok: bool
    method: str
    stderr: str = ""
    orphans_reaped: int = 0


# Tunables — overridable for tests.
_ACPI_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.25
# Per-call subprocess.run timeout. Independent of the ACPI poll budget so
# a single hung virsh invocation can't stall the close button forever.
_VIRSH_CALL_TIMEOUT_S = 8.0

# Sentinel returned by _domain_state when virsh itself failed transiently
# (libvirtd unreachable, virsh binary missing, etc.) — distinguished from
# "domain truly doesn't exist" (returncode==0 with empty stdout, or a
# specific "domain not found" stderr).
DOMAIN_STATE_ERROR = "__virsh_error__"


def _virsh(argv: Sequence[str], *, libvirt_uri: str = "qemu:///session",
           runner=subprocess.run,
           timeout: float = _VIRSH_CALL_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Thin virsh wrapper. ``runner`` is injectable for tests.

    A ``--`` separator is inserted before the subcommand's positional
    arguments so a vm_name like ``--all`` can't be re-parsed as a virsh
    option. ``timeout`` defends against a hung libvirtd; on
    TimeoutExpired we synthesise a non-zero CompletedProcess that the
    caller can detect via the sentinel state.
    """
    cmd = ["virsh", "--connect", libvirt_uri, *argv]
    try:
        return runner(cmd, capture_output=True, text=True, check=False,
                      timeout=float(timeout))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124,
            stdout="", stderr=f"virsh timed out after {timeout}s")
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            args=cmd, returncode=127,
            stdout="", stderr="virsh not found on PATH")
    except TypeError:
        # Test runners with no `timeout=` kwarg (older test harness shape).
        # Fall back to the un-timed call.
        return runner(cmd, capture_output=True, text=True, check=False)


def _virsh_domstate_argv(vm_name: str) -> list[str]:
    return ["domstate", "--", str(vm_name)]


def _domain_state(vm_name: str, *, libvirt_uri: str = "qemu:///session",
                  runner=subprocess.run) -> str:
    """Return ``virsh domstate`` output (lowercased, trimmed).

    Three return shapes the caller distinguishes:
      - ``"running"`` / ``"shut off"`` / ``"paused"`` — real domain states
      - ``""`` — domain not found (virsh rc==0 with empty stdout, or
        the stderr matches a "no such domain" pattern)
      - :data:`DOMAIN_STATE_ERROR` — virsh failed transiently (libvirt
        unreachable, virsh missing, timeout). Callers must NOT treat this
        as "domain gone" — that's what produces the silent-false-success
        path the operational review flagged.
    """
    proc = _virsh(_virsh_domstate_argv(vm_name),
                  libvirt_uri=libvirt_uri, runner=runner)
    stdout = (proc.stdout or "").strip().lower()
    stderr = (proc.stderr or "").strip().lower()
    if proc.returncode == 0:
        if stdout:
            return stdout
        # Some virsh builds report "domain not found" with rc==0 + non-
        # empty stderr; if stderr says that, treat as truly missing.
        if "not found" in stderr or "no such" in stderr:
            return ""
        # rc==0, no stdout, no telltale stderr — could be either; lean
        # toward "domain gone" because that's what virsh historically
        # returned for absent domains.
        return ""
    # rc != 0 — distinguish "domain not found" from transient failure
    # (libvirtd unreachable, virsh missing, timeout). The "failed to get
    # domain" / "Domain not found" pattern is the canonical virsh
    # diagnostic for an absent domain.
    if ("failed to get domain" in stderr
            or "domain not found" in stderr
            or "no such domain" in stderr):
        return ""
    return DOMAIN_STATE_ERROR


def _reap_orphans(vm_name: str, *,
                  ps_runner=subprocess.run,
                  kill_fn=os.kill) -> int:
    """Send SIGTERM to any lingering qdistro-forward / qemu helper.

    Best-effort: we match on ``qdistro-forward`` argv carrying the vm
    name. vm_name is `re.escape`'d so a hostile-or-buggy caller that
    routed past validation can't expand into a wide-matching regex.
    Returns the number of pids signalled.
    """
    # Defence-in-depth: caller already validated, but escape anyway so a
    # future change that bypasses validation doesn't mass-SIGTERM.
    safe = re.escape(str(vm_name))
    try:
        proc = ps_runner(["pgrep", "-f", f"qdistro-forward.*{safe}"],
                         capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 0
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    n = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        try:
            kill_fn(int(line), signal.SIGTERM)
            n += 1
        except (ProcessLookupError, PermissionError):
            continue
    return n


def close_vm(vm_name: str, *,
             libvirt_uri: str = "qemu:///session",
             acpi_timeout_s: float = _ACPI_TIMEOUT_S,
             poll_interval_s: float = _POLL_INTERVAL_S,
             runner=subprocess.run,
             sleep_fn=time.sleep,
             clock_fn=time.monotonic,
             ps_runner=subprocess.run,
             kill_fn=os.kill) -> CloseResult:
    """Shut a tier-4 VM down for the close button.

    Strategy (per task P05a Phase B answers): send ACPI shutdown via
    ``virsh shutdown --mode acpi``, poll ``virsh domstate`` for up to
    ``acpi_timeout_s`` seconds, then fall through to
    ``virsh destroy`` if the guest is still alive. Reap any orphan
    ``qdistro-forward`` peer at the end.

    Every external dep (virsh, pgrep, time, kill) is injectable so the
    unit tests can drive the full state machine without a libvirt or
    real qemu in the loop.
    """
    if not _vm_name_is_safe(vm_name):
        _close_log("close_vm reject", vm_name=vm_name, reason="invalid-name")
        return CloseResult(ok=False, method="missing",
                           stderr=f"invalid vm_name {vm_name!r}")

    _close_log("close_vm start", vm_name=vm_name)

    # Already gone? Distinguish "truly absent" (return ok=True) from
    # "virsh transient failure" (return ok=False so the UI surfaces the
    # broken state instead of silently claiming success).
    state = _domain_state(vm_name, libvirt_uri=libvirt_uri, runner=runner)
    if state == DOMAIN_STATE_ERROR:
        _close_log("close_vm domain-probe failed",
                   vm_name=vm_name, outcome="not-ok",
                   reason="virsh-transient")
        return CloseResult(ok=False, method="missing",
                           stderr="virsh domstate failed transiently")
    if not state or state in ("shut off", "shutoff"):
        reaped = _reap_orphans(vm_name, ps_runner=ps_runner, kill_fn=kill_fn)
        _close_log("close_vm done", vm_name=vm_name, outcome="ok",
                   method="missing", orphans=reaped, reason="already-off")
        return CloseResult(ok=True, method="missing", orphans_reaped=reaped)

    # ACPI shutdown attempt. The `--` separator ensures vm_name can't be
    # mis-parsed as a virsh option even if validation evolves.
    acpi_proc = _virsh(["shutdown", "--mode", "acpi", "--", vm_name],
                       libvirt_uri=libvirt_uri, runner=runner)
    acpi_stderr = (acpi_proc.stderr or "").strip()

    deadline = clock_fn() + float(acpi_timeout_s)
    while clock_fn() < deadline:
        state = _domain_state(vm_name, libvirt_uri=libvirt_uri, runner=runner)
        if state == DOMAIN_STATE_ERROR:
            # Transient failure mid-poll: keep polling. Treat as "still
            # alive" — only confirmed shutoff or a hard destroy moves us
            # to success.
            sleep_fn(float(poll_interval_s))
            continue
        if not state or state in ("shut off", "shutoff"):
            reaped = _reap_orphans(vm_name,
                                   ps_runner=ps_runner, kill_fn=kill_fn)
            _close_log("close_vm done", vm_name=vm_name, outcome="ok",
                       method="acpi", orphans=reaped, reason="acpi-shutdown")
            return CloseResult(ok=True, method="acpi",
                               stderr=acpi_stderr,
                               orphans_reaped=reaped)
        sleep_fn(float(poll_interval_s))

    # Hard destroy.
    destroy_proc = _virsh(["destroy", "--", vm_name],
                          libvirt_uri=libvirt_uri, runner=runner)
    destroy_stderr = (destroy_proc.stderr or "").strip()
    # Confirm.
    state = _domain_state(vm_name, libvirt_uri=libvirt_uri, runner=runner)
    if state == DOMAIN_STATE_ERROR:
        # Can't confirm — surface as not-ok rather than lying.
        alive = True
        reaped = _reap_orphans(vm_name, ps_runner=ps_runner, kill_fn=kill_fn)
        _close_log("close_vm done", vm_name=vm_name, outcome="not-ok",
                   method="destroy", orphans=reaped,
                   reason="destroy-confirm-failed")
        return CloseResult(
            ok=False, method="destroy",
            stderr=("\n".join(s for s in (acpi_stderr, destroy_stderr,
                                          "destroy confirm: virsh transient failure") if s)),
            orphans_reaped=reaped,
        )
    alive = bool(state) and state not in ("shut off", "shutoff")
    reaped = _reap_orphans(vm_name, ps_runner=ps_runner, kill_fn=kill_fn)
    outcome = "ok" if not alive else "not-ok"
    _close_log("close_vm done", vm_name=vm_name, outcome=outcome,
               method="destroy", orphans=reaped,
               reason=("destroyed" if not alive else "destroy-failed"))
    return CloseResult(
        ok=(not alive),
        method="destroy",
        stderr=("\n".join(s for s in (acpi_stderr, destroy_stderr) if s)),
        orphans_reaped=reaped,
    )


def _close_log(event: str, **fields) -> None:
    """Structured log line for the close-button journal trail.

    Format:
      [tier4-vm/close] <event> key=value …
    Suppressed when ``QDISTRO_TIER4_QUIET=1`` so unit tests aren't noisy.
    """
    if os.environ.get("QDISTRO_TIER4_QUIET", "") == "1":
        return
    parts = [f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}"
             for k, v in fields.items()]
    line = f"[tier4-vm/close] {event} " + " ".join(parts)
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ---- clipboard MIME strip ----

# Tier-4 ↔ other-tier clipboard transfers are restricted to text-only
# MIMEs. Mirrors the existing tier-3 clipboard policy: anything that
# could carry a VM-side exploit (image bytes hitting a decoder, HTML
# with embedded handlers, x-special/gnome-copied-files dragging the
# desktop into the silo) gets stripped at the gate.
ALLOWED_MIMES = frozenset({
    "text/plain",
    "text/uri-list",
})


def strip_mimes(mimes: Sequence[str]) -> list[str]:
    """Return ``mimes`` filtered to the tier-4 allow-list, preserving order.

    The list shape is what ClipboardGate.qml passes us via newline-
    joined string; we keep this as a plain Python list so the bats
    driver can shell it through ``python3 -c`` without serialisation
    drama. Pure function — no side effects.
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in mimes:
        s = str(m or "").strip()
        if not s:
            continue
        # text/plain;charset=utf-8 etc. — match on the base type.
        base = s.split(";", 1)[0].strip().lower()
        if base in ALLOWED_MIMES and s not in seen:
            out.append(s)
            seen.add(s)
    return out


def have_virsh() -> bool:
    """Cheap probe for callers that want to degrade gracefully."""
    return shutil.which("virsh") is not None
