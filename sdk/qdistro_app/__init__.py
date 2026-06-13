"""qdistro_app — Python SDK for user apps to interact with the admin
broker and with peer user silos.

Phase 1 surface was a single `request()` call. Phase 3 added a minimal
receiver mixin for the `org.qdistro.App1` cross-user contract, plus
helpers for enumerating peer receivers and sending to one. P03 extends
the App1 surface with the app-launcher contract:

- ``GetName()`` — friendly name surfaced in the Send-To menu and the
  qdshell PodApps launcher.
- ``GetSilo()`` — silo badge label (`work`, `personal`, …) so PodApps
  can group / colour entries the same way SessionManager1 does.
- ``CanReceive(kind)`` — gate "what content types does this app
  accept?" so the Send-To UI can grey out non-applicable targets
  client-side before paying the admin-approval round-trip.
- ``ReceivePayload(payload)`` — companion to ``Receive(kind, payload)``
  for kind-less drops (e.g. clipboard paste, file open). Treated as
  ``Receive("application/octet-stream", payload)`` internally so
  ``GetLastReceived`` / ``last_received`` stay one observable.
- ``PayloadReceived`` signal — qdshell uses this to drop the
  "delivering…" toast on the sender side once the receiver acks.

Apps wire themselves via the higher-level ``register_app`` helper in
``qdistro_app.app_receiver``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable

import dbus
import dbus.service

_BUS_NAME = "org.qdistro.AdminBroker1"
_OBJ_PATH = "/org/qdistro/AdminBroker1"

# The shipped trusted launch path for tier-2 / disposable silos. The SDK helper
# below execs it; the binary re-does every gate (class resolution, the
# qdistro.dispose.open: broker gate, the RO-input validation) so the SDK is a
# convenience layer, never the security boundary.
_TIER2_SPAWN_BIN = "qdistro-tier2-spawn"

APP1_IFACE = "org.qdistro.App1"
APP1_OBJ_PATH = "/org/qdistro/App1"

DEFAULT_TIMEOUT_S = 600

# Kind sentinel used when the sender calls the kind-less ``ReceivePayload``
# variant. Receivers see this exactly as if the sender had passed
# ``Receive("application/octet-stream", payload)``, keeping
# ``GetLastReceived`` / ``last_received`` one observable across both
# entry points.
DEFAULT_KIND = "application/octet-stream"


def request(action: str, details: dict | None = None, *,
            timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    """Request a permission from admin. Blocks until admin decides
    or `timeout` seconds pass.

    timeout is the per-call D-Bus reply timeout on WaitForDecision.
    The default 600s (10 minutes) balances "admin is at lunch" with
    "calling app shouldn't hang forever"; pass a smaller value for
    interactive paths where a stale request is worse than no answer.
    Returns True if approved, False if denied or timed out.
    """
    bus = dbus.SystemBus()
    broker = bus.get_object(_BUS_NAME, _OBJ_PATH)
    rid = int(broker.RequestPermission(str(action), details or {}, dbus_interface=_BUS_NAME))
    allowed = broker.WaitForDecision(rid, dbus_interface=_BUS_NAME, timeout=float(timeout))
    return bool(allowed)


class OpenInDisposableError(RuntimeError):
    """open_in_disposable() could not launch the disposable (class disabled /
    unknown, input invalid, broker refused, or the spawn binary failed). The
    message carries the spawn binary's stderr where available."""


def open_in_disposable(path: str, *, class_name: str,
                       spawn_bin: str | None = None,
                       extra_env: dict[str, str] | None = None) -> dict:
    """Open ``path`` read-only inside a fresh tier-2 disposable for ``class_name``
    (07-disposables-plan P2 — "open this thing in a disposable").

    This is a thin convenience over the SHIPPED trusted launch binary
    (``qdistro-tier2-spawn --disposable``). The binary — NOT this helper — is
    the security boundary: it resolves ``class_name`` from the disposable-class
    registry (enforcing the ``min_tier`` hostile-class gate), pins the workload
    + network from the class, validates the input, calls the broker
    ``qdistro.dispose.open:<class>`` gate (rules-only / fail-closed), and binds
    the input read-only under ``/mnt/input/<basename>``. A compromised caller
    that skips this helper still hits every one of those gates in the binary.

    ``path`` must be an existing absolute file or directory. Raises
    :class:`OpenInDisposableError` on any failure (the class is disabled/unknown,
    the input is invalid, the broker refuses, or the binary errors); the
    container is launched in the background (it owns its own lifecycle / lease),
    and a dict of the parsed ``KEY=VALUE`` launch contract
    (``LAUNCH_TOKEN``/``CONTAINER``/``IMAGE``/``APP_ID``) is returned on success.

    The class registry is consulted SERVER-SIDE by the binary; we deliberately
    do not re-implement the gate here (a second copy could drift and lie). We do
    a cheap client-side path sanity check so an obvious mistake fails fast with a
    clear error before paying a process spawn.
    """
    if not isinstance(path, str) or not path:
        raise OpenInDisposableError("path must be a non-empty string")
    if not os.path.isabs(path):
        raise OpenInDisposableError(f"path must be absolute: {path!r}")
    real = os.path.realpath(path)
    if not os.path.exists(real):
        raise OpenInDisposableError(f"path does not exist: {path!r}")
    if not (os.path.isfile(real) or os.path.isdir(real)):
        raise OpenInDisposableError(
            f"path is neither a regular file nor a directory: {path!r}")
    if not isinstance(class_name, str) or not class_name:
        raise OpenInDisposableError("class_name must be a non-empty string")

    binary = spawn_bin or shutil.which(_TIER2_SPAWN_BIN) or _TIER2_SPAWN_BIN

    env = dict(os.environ)
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    env["TIER2_OPEN_CLASS"] = class_name
    env["TIER2_RO_INPUT"] = real

    # The binary's signature is `--disposable <workload> -- <app> [args]`, so we
    # must name the workload. We resolve it from the SHIPPED class registry (no
    # second registry copy in the SDK). This is NOT a trust point: the binary
    # re-validates that the class's registry workload equals this one and
    # refuses on a mismatch.
    workload, app_argv = _resolve_open_workload(class_name, env)

    cmd = [binary, "--disposable", workload, "--", *app_argv]
    try:
        # Background: the disposable owns its own lifecycle (window-close + --rm,
        # or a TTL lease). We capture the pre-exec stdout contract by running
        # with TIER2_DETACH so the binary emits the contract then returns.
        env["TIER2_DETACH"] = "1"
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OpenInDisposableError(
            f"failed to launch {binary}: {e}") from e
    if proc.returncode != 0:
        raise OpenInDisposableError(
            f"{binary} refused/failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()}")
    contract: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k in ("LAUNCH_TOKEN", "CONTAINER", "IMAGE", "APP_ID"):
                contract[k] = v
    return contract


def _resolve_open_workload(class_name: str,
                           env: dict[str, str]) -> tuple[str, list[str]]:
    """Ask the shipped class-registry resolver for the workload backing
    ``class_name`` (so the SDK doesn't carry a second copy of the registry).
    Returns ``(workload, app_argv)``. Raises :class:`OpenInDisposableError` if
    the class is unknown/disabled or the registry is malformed (the resolver's
    non-zero exit). The binary re-validates server-side regardless."""
    resolver = None
    candidates = []
    override = env.get("QDISTRO_DISPOSABLE_CLASSES_RESOLVER")
    if override:
        candidates.append(override)
    candidates.append("/usr/libexec/qdistro/qdistro_disposable_classes.py")
    for cand in candidates:
        if os.path.exists(cand):
            resolver = cand
            break
    if resolver is None:
        raise OpenInDisposableError(
            "disposable-class registry resolver not installed "
            "(/usr/libexec/qdistro/qdistro_disposable_classes.py)")
    try:
        proc = subprocess.run(
            ["python3", resolver, "--resolve", class_name],
            env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OpenInDisposableError(f"class resolve failed: {e}") from e
    if proc.returncode != 0:
        raise OpenInDisposableError(
            f"class {class_name!r} is not openable "
            f"(resolver rc={proc.returncode}): {proc.stderr.strip()}")
    workload = ""
    for line in proc.stdout.splitlines():
        if line.startswith("WORKLOAD="):
            workload = line.partition("=")[2]
            break
    if not workload:
        raise OpenInDisposableError(
            f"resolver returned no workload for class {class_name!r}")
    # Default app argv: the workload's own entrypoint. For the shipped
    # weston-terminal scratch workload this is the terminal; richer classes
    # (text-viewer, url-preview) ship their own image entrypoint, so an empty
    # app argv would be wrong — we run the workload name as the app, matching
    # how the image's PATH exposes it. Callers needing a specific argv can use
    # qdistro-tier2-spawn directly.
    return workload, [workload]


def _resolve_silo() -> str:
    """Best-effort silo label for the current process.

    Honours ``$QDISTRO_SILO`` first (set by the launcher / spawn
    helpers when they exec the app into a silo's runtime dir). Falls
    back to the unix username; admin (uid 1000 on test bakes) is
    surfaced as ``admin`` for parity with what SessionManager1
    advertises in its silos.yaml.
    """
    env = os.environ.get("QDISTRO_SILO", "").strip()
    if env:
        return env
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001
        return ""


class AppReceiver(dbus.service.Object):
    """Claims a session-bus name and exposes ``org.qdistro.App1``.

    The Phase-3 minimum surface (``Receive(kind, payload)`` plus
    ``GetLastReceived``) is kept verbatim so existing stubs and tests
    don't break. P03 layers the launcher-contract methods on top:

    - ``GetName()`` returns ``friendly_name`` (defaulted from the
      service name's tail).
    - ``GetSilo()`` returns whatever the launcher injected via
      ``QDISTRO_SILO`` (falls back to the unix username).
    - ``CanReceive(kind)`` returns True for any kind in the
      ``supported_kinds`` list, or for ``"*"``-style wildcards. The
      default list is ``("*",)`` so legacy receivers stay accepting.
    - ``ReceivePayload(payload)`` is the kind-less companion to
      ``Receive``; it dispatches as
      ``Receive(DEFAULT_KIND, payload)`` so observers don't need
      two code paths.
    - ``PayloadReceived`` is emitted after every successful receive
      (both ``Receive`` and ``ReceivePayload`` entry points) so the
      sender's qdshell toast can clear without polling.

    Subclass and override ``on_receive`` to handle incoming messages;
    the D-Bus-thread call hops to your callback which MUST be
    thread-safe or marshal onto Qt's main thread
    (``QTimer.singleShot(0, ...)``) itself.
    """

    OBJ_PATH = APP1_OBJ_PATH

    def __init__(self, service_name: str,
                 on_receive: Callable[[str, str], None],
                 bus: dbus.Bus | None = None,
                 *,
                 friendly_name: str | None = None,
                 silo: str | None = None,
                 supported_kinds: Iterable[str] | None = None):
        if bus is None:
            bus = dbus.SessionBus()
        # Claim the bus name before registering the object — a name
        # collision here means another instance is already running
        # for this uid under this service name, and silently shadowing
        # it would make debugging painful.
        self._bus_name = dbus.service.BusName(
            service_name, bus, do_not_queue=True)
        super().__init__(bus, self.OBJ_PATH)
        self._service_name = str(service_name)
        self._on_receive = on_receive
        self._friendly_name = (str(friendly_name)
                               if friendly_name
                               else _friendly_from_service(service_name))
        self._silo = str(silo) if silo is not None else _resolve_silo()
        self._supported_kinds = tuple(supported_kinds) if supported_kinds else ("*",)
        # Phase 4 plugins want a cheap "did it arrive?" for headless
        # tests without rigging an app-specific GetDocument each time.
        # Track the most recent (kind, payload) on the receiver and
        # expose it via GetLastReceived — generic enough to be useful
        # for qterminator (PTY text), qnotebook (markdown text), and
        # any future App1 participant. ``Receive`` and
        # ``ReceivePayload`` both update it before dispatching to the
        # caller's on_receive callback, so even if on_receive raises,
        # the probe still reports what landed.
        self._last_received: tuple[str, str] | None = None

    # ---- properties ------------------------------------------------

    @property
    def service_name(self) -> str:
        return self._service_name

    @property
    def friendly_name(self) -> str:
        return self._friendly_name

    @property
    def silo(self) -> str:
        return self._silo

    @property
    def supported_kinds(self) -> tuple[str, ...]:
        return self._supported_kinds

    @property
    def last_received(self) -> tuple[str, str] | None:
        return self._last_received

    # ---- D-Bus methods ---------------------------------------------

    @dbus.service.method(APP1_IFACE, in_signature="ss", out_signature="")
    def Receive(self, kind: str, payload: str) -> None:
        self._deliver(str(kind), str(payload))

    @dbus.service.method(APP1_IFACE, in_signature="s", out_signature="")
    def ReceivePayload(self, payload: str) -> None:
        """Kind-less variant. Treated as ``Receive(DEFAULT_KIND, payload)``
        so observers (``last_received``, ``GetLastReceived``,
        ``PayloadReceived`` signal) see a single normalised entry."""
        self._deliver(DEFAULT_KIND, str(payload))

    @dbus.service.method(APP1_IFACE, in_signature="", out_signature="s")
    def GetName(self) -> str:
        return self._friendly_name

    @dbus.service.method(APP1_IFACE, in_signature="", out_signature="s")
    def GetSilo(self) -> str:
        return self._silo

    @dbus.service.method(APP1_IFACE, in_signature="s", out_signature="b")
    def CanReceive(self, kind: str) -> bool:
        return _kind_accepted(self._supported_kinds, str(kind))

    @dbus.service.method(APP1_IFACE, in_signature="", out_signature="s")
    def GetLastReceived(self) -> str:
        """Return the most recent payload in `[kind] payload` form, or
        empty string if nothing has been received yet. Formatted to
        match qstub-notepad's GetDocument output so assertions can be
        shared across Phase 3 and Phase 4 scenarios."""
        if self._last_received is None:
            return ""
        k, p = self._last_received
        return f"[{k}] {p}"

    @dbus.service.signal(APP1_IFACE, signature="ss")
    def PayloadReceived(self, kind: str, payload: str) -> None:
        # Body intentionally empty — dbus.service.signal wraps method
        # bodies as no-ops; the decorator handles the emit. Keeping the
        # body explicit so refactors don't accidentally drop the
        # signature.
        pass

    # ---- internal --------------------------------------------------

    def _deliver(self, kind: str, payload: str) -> None:
        self._last_received = (kind, payload)
        try:
            self._on_receive(kind, payload)
        finally:
            # Emit even if on_receive raised — the wire-level "arrived"
            # event is independent of whatever the app does with it,
            # and qdshell's toast clear should not stick on a buggy app.
            try:
                self.PayloadReceived(kind, payload)
            except Exception:  # noqa: BLE001
                # Signal emission on a torn-down bus shouldn't kill the
                # whole receiver process; the worst case is a stuck
                # toast that GCs out with placeholderTimeoutMs.
                pass


def _friendly_from_service(service: str) -> str:
    """Best-effort friendly name derived from a org.qdistro.<Name>[.uidNNNN]
    bus name. Mirrors qdistro_user_relay._friendly_name so the
    launcher and the receiver agree on what to display."""
    name = str(service)
    prefix = "org.qdistro."
    if name.startswith(prefix):
        name = name[len(prefix):]
    parts = name.rsplit(".", 1)
    if len(parts) == 2 and parts[1].startswith("uid") and parts[1][3:].isdigit():
        name = parts[0]
    return name


def _kind_accepted(supported: tuple[str, ...], kind: str) -> bool:
    """Return True iff ``kind`` matches one of the supported entries.

    ``"*"`` accepts anything. ``"text/*"`` (trailing-star prefix) accepts
    every kind that starts with the literal prefix before the star.
    Otherwise an exact case-sensitive match.
    """
    k = str(kind)
    for entry in supported:
        if entry == "*":
            return True
        if entry.endswith("/*"):
            prefix = entry[:-1]  # keep the trailing slash
            if k.startswith(prefix):
                return True
        if entry == k:
            return True
    return False


def list_receivers() -> list[tuple[int, str, str]]:
    """Return (uid, service_name, friendly_name) for every receiver
    currently registered across all running user sessions.

    Calls the broker's ListReceivers (root-only side-channel that
    can peek into every uid's session bus). No admin approval
    required for enumeration — gate is on RelayMessage.
    """
    bus = dbus.SystemBus()
    broker = bus.get_object(_BUS_NAME, _OBJ_PATH)
    rows = broker.ListReceivers(dbus_interface=_BUS_NAME)
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def send_to(target_uid: int, target_service: str,
            kind: str, payload: str, *,
            timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    """Ask admin to relay a message to `target_service` running as
    `target_uid`. Blocks until admin decides or timeout expires.

    Returns True iff the target's Receive(kind, payload) was invoked
    (admin approved and the relay delivered). DBusException bubbles
    up on deny or relay failure.
    """
    bus = dbus.SystemBus()
    broker = bus.get_object(_BUS_NAME, _OBJ_PATH)
    broker.RelayMessage(
        dbus.Int32(int(target_uid)),
        dbus.String(str(target_service)),
        dbus.String(str(kind)),
        dbus.String(str(payload)),
        dbus_interface=_BUS_NAME,
        timeout=float(timeout),
    )
    return True
