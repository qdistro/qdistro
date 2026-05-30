"""Wire the tier-5b per-app VM launcher into qdistro's App1 contract.

Tier-5b VMs are short-lived: one VM per app instance. This module
implements the small App1 helper that qdshell's PodApps service
expects so that "Firefox (tier-5b)" can appear in the launcher and
the broker can find it for placeholder correlation.

Each running tier-5b VM claims a bus name of the form

    org.qdistro.Tier5bVM.uid<NNNN>

on the session bus. The placeholder-correlator in qdshell's PodApps
service matches the per-spawn ``LAUNCH_TOKEN`` echoed by
``spawn-tier5b.sh`` against the inner xdg_toplevel's secctx
instance_id (which the host-side ``qdistro-secctx-exec`` wrap stamps
onto the outer wl_client) and resolves the placeholder to the new
toplevel.

The helper degrades to a no-op when ``dbus-python`` is missing or the
session bus isn't reachable, matching the pattern used by every other
App1-registered app (qfileman, qterminator, qnotebook, qdbrowser).

Public entry-points:

- :func:`maybe_install` — call from qdshell's tier-5b launcher
  process after it has spawned ``spawn-tier5b.sh``. Returns the
  receiver handle (or ``None``).
- :func:`build_launcher_argv` — pure function that assembles the
  argv qdshell uses to spawn ``spawn-tier5b.sh`` with the right
  secctx triple. Exposed for unit tests so we don't have to fork a
  shell to verify argv assembly.
- :func:`expected_service_name` — pure function returning the
  ``org.qdistro.Tier5bVM.uidNNNN`` name the launcher will claim for
  the running uid. Used by tests and by PodApps' placeholder
  correlator.
"""
from __future__ import annotations

import os
import shlex
import sys
from typing import Sequence

try:  # pragma: no cover — App1 SDK only available in qdistro deployments
    from qdistro_app import app_receiver as _app_receiver
except ImportError:
    _app_receiver = None  # type: ignore[assignment]


APP_FRIENDLY_NAME = "Tier5bVM"
APP_SUPPORTED_KINDS = ("text/plain", "text/uri-list")

# Default app the launcher targets when none is supplied. The first
# tier-5b deployment shipped is Firefox (probe verdict
# §"First app: Firefox over a CUPS spike. Recommend yes").
DEFAULT_APP = "firefox"

# Default sandbox engine — what qdistro-secctx-exec stamps on the
# outer wl_client. Mirrors tier-5's `qdistro.tier5` but namespaced.
DEFAULT_SECCTX_ENGINE = "qdistro.tier5b"


def expected_service_name(uid: int | None = None) -> str:
    """Return the bus name the tier-5b launcher claims.

    Format: ``org.qdistro.Tier5bVM.uid<NNNN>``.
    """
    u = os.geteuid() if uid is None else int(uid)
    return f"org.qdistro.{APP_FRIENDLY_NAME}.uid{u}"


def build_launcher_argv(
    *,
    vm_name: str,
    app: str = DEFAULT_APP,
    spawn_script: str = "/usr/libexec/qdistro/spawn-tier5b.sh",
    extra_app_args: Sequence[str] | None = None,
    secctx_engine: str = DEFAULT_SECCTX_ENGINE,
    secctx_app_id: str | None = None,
    secctx_instance: str | None = None,
) -> list[str]:
    """Assemble the argv to spawn one tier-5b VM publishing ``app``.

    The returned argv is what qdshell's launcher passes to
    ``subprocess.Popen`` (with the env vars for the secctx triple
    pre-set; see :func:`build_launcher_env`).

    Pure function — no side effects, no I/O. Tested by
    ``tests/unit/test_tier5b_publisher.py``.
    """
    if not vm_name or not isinstance(vm_name, str):
        raise ValueError("vm_name must be a non-empty string")
    if "/" in vm_name or " " in vm_name:
        raise ValueError(f"vm_name {vm_name!r} contains forbidden chars")
    if not app or not isinstance(app, str):
        raise ValueError("app must be a non-empty string")
    argv = [
        spawn_script,
        "--vm", vm_name,
        "--app", app,
    ]
    if extra_app_args:
        argv.append("--")
        for a in extra_app_args:
            if not isinstance(a, str):
                raise TypeError(f"extra_app_args element not str: {a!r}")
            argv.append(a)
    # Validate the optional secctx overrides up front (TypeError on bad
    # values is friendlier than a downstream shell failure).
    if secctx_engine and not isinstance(secctx_engine, str):
        raise TypeError("secctx_engine must be a string")
    if secctx_app_id is not None and not isinstance(secctx_app_id, str):
        raise TypeError("secctx_app_id must be a string or None")
    if secctx_instance is not None and not isinstance(secctx_instance, str):
        raise TypeError("secctx_instance must be a string or None")
    return argv


def build_launcher_env(
    *,
    vm_name: str,
    app: str = DEFAULT_APP,
    secctx_engine: str = DEFAULT_SECCTX_ENGINE,
    secctx_app_id: str | None = None,
    secctx_instance: str | None = None,
) -> dict[str, str]:
    """Build the env dict spawn-tier5b.sh expects to receive.

    Returned dict overlays onto the parent env (not a replacement). The
    caller is expected to `env.update(build_launcher_env(...))`.

    Secctx app_id defaults to ``qdistro.tier5b.<vm_name>`` (matches
    spawn-tier5b.sh's internal default but explicit at the launcher
    layer so qdshell can correlate placeholder vs. arrival).
    """
    if not vm_name:
        raise ValueError("vm_name must be set")
    env = {
        "TIER5B_APP": str(app),
        "TIER5B_SECCTX_ENGINE": str(secctx_engine),
    }
    env["TIER5B_SECCTX_APPID"] = (
        secctx_app_id
        if secctx_app_id is not None
        else f"qdistro.tier5b.{vm_name}"
    )
    if secctx_instance is not None:
        env["TIER5B_SECCTX_INSTANCE"] = secctx_instance
    return env


def shell_quote_argv(argv: Sequence[str]) -> str:
    """Helper for diagnostic logging — POSIX-quote an argv list."""
    return " ".join(shlex.quote(str(a)) for a in argv)


def maybe_install(window=None) -> object | None:
    """Register the tier-5b launcher with qdshell's App1 contract.

    ``window`` is unused (kept positional for parity with other apps'
    ``maybe_install(window)`` signature); tier-5b has no GUI surface
    of its own — each spawned VM owns one or more xdg_toplevels
    chrome-painted by qdwin.

    Returns the registration handle, or ``None`` if the SDK isn't
    importable / no session bus is available. The receiver is
    deliberately a no-op on inbound payload (tier-5b VMs don't
    accept Send-To deliveries in v1; that's a future feature).

    App1-receiver-registration decision (2026-05-30 — was an open
    watch-list question in todo/open-followups.md):
      * Today this helper has ZERO callers — no qdshell launcher calls
        ``maybe_install()`` — so the ``org.qdistro.Tier5bVM.uidNNNN``
        bus name is NOT actually claimed for a running tier-5b VM, and
        must NOT be treated as launch evidence.
      * The registration is intentionally EPHEMERAL: there is no
        NameLost / re-register-on-name-loss handler, and if the session
        bus restarts the name is simply dropped. That is acceptable
        because (a) the receiver is a v1 no-op that only drops Send-To
        deliveries, so losing it costs nothing, and (b) the
        load-bearing launch→toplevel correlation runs over the per-spawn
        ``LAUNCH_TOKEN`` / secctx ``instance_id`` path
        (see :func:`expected_service_name`'s callers and the module
        docstring), not over bus-name liveness.
      * If/when Send-To-into-a-tier-5b-VM ships, re-registration on name
        loss should be added THEN, alongside an actual caller — not
        speculatively now. No behaviour change is made here.
    """
    if _app_receiver is None:
        print("[tier5b/qdistro] qdistro_app SDK not importable; "
              "App1 registration skipped",
              file=sys.stderr, flush=True)
        return None

    def on_receive(kind: str, payload: str) -> None:
        # Tier-5b VMs are per-app, short-lived; Send-To delivery into a
        # specific VM is not yet supported. Log the drop loudly so we
        # surface "qdshell tried to send into a tier-5b VM and the
        # user lost the data" rather than failing silently.
        print(f"[tier5b/qdistro] received {kind} payload "
              f"({len(payload)} bytes) but tier-5b VMs don't accept "
              f"Send-To deliveries in v1; dropped",
              file=sys.stderr, flush=True)

    receiver = _app_receiver.register_app(
        APP_FRIENDLY_NAME,
        on_receive=on_receive,
        friendly_name=APP_FRIENDLY_NAME,
        supported_kinds=APP_SUPPORTED_KINDS,
    )
    if receiver is None:
        return None
    print(f"[tier5b/qdistro] App1 receiver registered as "
          f"{receiver.service_name} (silo={receiver.silo!r})",
          flush=True)
    return receiver
