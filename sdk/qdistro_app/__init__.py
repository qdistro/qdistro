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

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable, Iterable

import dbus
import dbus.service

log = logging.getLogger("qdistro_app")

_BUS_NAME = "org.qdistro.AdminBroker1"
_OBJ_PATH = "/org/qdistro/AdminBroker1"

# The session manager's D-Bus surface (SYSTEM bus) — the workflow-runner
# teardown method ``DisposeByWorkflow`` lives here, NOT on the AdminBroker.
_SM_BUS_NAME = "org.qdistro.SessionManager1"
_SM_OBJ_PATH = "/org/qdistro/SessionManager1"
_SM_IFACE = "org.qdistro.SessionManager1"

# A workflow lease id rides in a podman label + a podman --filter value, so the
# daemon (qdistro_disposables.is_workflow_id) and spawn-tier2.sh both constrain
# it to this conservative lowercase token shape. The SDK MUST validate any id it
# stamps into ``QDISTRO_DISPOSABLE_WORKFLOW``: spawn-tier2.sh *silently ignores*
# an invalid value, which would spawn an UNTAGGED disposable that no
# DisposeByWorkflow can ever reap — a silent leak. So an invalid id is a hard
# error here, BEFORE any spawn. Kept byte-identical to the daemon's regex.
_WORKFLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")

# The env var spawn-tier2.sh reads to opt a disposable into a workflow group
# (stamping the ``qdistro_lease_workflow=<id>`` podman label).
WORKFLOW_ENV = "QDISTRO_DISPOSABLE_WORKFLOW"

# The shipped trusted launch path for tier-2 / disposable silos. The SDK helper
# below execs it; the binary re-does every gate (class resolution, the
# qdistro.dispose.open: broker gate, the RO-input validation) so the SDK is a
# convenience layer, never the security boundary.
_TIER2_SPAWN_BIN = "qdistro-tier2-spawn"

APP1_IFACE = "org.qdistro.App1"
APP1_OBJ_PATH = "/org/qdistro/App1"

DEFAULT_TIMEOUT_S = 600

# Best-effort cleanup-path D-Bus timeout. The context-manager exit teardown
# (WorkflowRun / step __exit__) must not block a process for the full 600s
# default per group if the daemon is wedged — exit is best-effort and swallows
# errors anyway, so a short reply timeout caps the worst case at O(steps)*this.
CLEANUP_TIMEOUT_S = 30

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


def open_for_edit(path: str, *, class_name: str, request_silo: str,
                  spawn_bin: str | None = None,
                  extra_env: dict[str, str] | None = None) -> dict:
    """Open ``path`` FOR EDITING in a fresh tier-2 disposable (the export-back
    edit-round-trip). Same trusted-binary boundary as :func:`open_in_disposable`,
    plus the per-launch export+edit opt-in: the binary requires ``class_name`` to
    be edit-capable, binds the source RO at ``/mnt/input/<name>`` AND a RW staging
    dir at ``/mnt/output``, and stamps the meta so that when the disposable is
    later imported (admin ``ImportFromDisposable``), the SINGLE file the editor
    saved to ``/mnt/output`` is promoted back BESIDE the source as
    ``<name>.disp-edited`` — never overwriting ``path`` in place.

    ``request_silo`` is the silo the edited copy lands in; the importer requires
    the source to live strictly under that silo's state (no cross-silo write), so
    pass the silo that owns ``path``. ``path`` must be an existing absolute
    regular FILE (a directory has no single source to edit). Returns the launch
    contract dict (as :func:`open_in_disposable`). Raises
    :class:`OpenInDisposableError` on any failure — the binary re-validates every
    gate, so this helper is convenience, never the security boundary."""
    if not isinstance(request_silo, str) or not request_silo:
        raise OpenInDisposableError("request_silo must be a non-empty string")
    real = os.path.realpath(path) if isinstance(path, str) and path else ""
    if not real or not os.path.isfile(real):
        # Mirror open_in_disposable's early sanity check but tightened to a
        # regular file (edit-round-trip is single-file); the binary re-checks.
        raise OpenInDisposableError(
            f"open_for_edit requires an existing regular file: {path!r}")
    env_extra = {
        "TIER2_REQUEST_SILO": request_silo,
        "TIER2_REQUEST_EDIT": "1",
    }
    if extra_env:
        env_extra.update({str(k): str(v) for k, v in extra_env.items()})
    return open_in_disposable(
        path, class_name=class_name, spawn_bin=spawn_bin, extra_env=env_extra)


# Kind tag for the edit-round-trip "your edited copy is ready" App1 message. The
# payload is a small JSON object naming the source + the landed copy so the
# source app can offer "open / replace" without re-reading the importer's stdout.
EDIT_READY_KIND = "application/x-qdistro-edit-ready"


def notify_edit_ready(target_uid: int, target_service: str, receipt: dict, *,
                      timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    """Notify the source app that an edit-round-trip landed: relay an App1
    message (kind :data:`EDIT_READY_KIND`) to ``target_service`` carrying the
    edit receipt's ``source``/``dest``/``sha256`` so the app can surface "an
    edited copy of <source> is ready" and offer to open or replace it.

    This is the SDK half of "source app notified via SDK"; the full qdshell /
    qfileman UI wiring is a separate backlog item. ``receipt`` is the dict
    returned by ``ImportFromDisposable`` for an ``edit`` import (``mode ==
    "edit"``). Returns True iff the relay delivered (admin approved + the target's
    Receive was invoked); a non-edit / dest-less receipt raises ValueError (there
    is nothing to announce)."""
    if not isinstance(receipt, dict) or receipt.get("mode") != "edit":
        raise ValueError("notify_edit_ready needs an edit-mode import receipt")
    dest = receipt.get("dest")
    if not dest:
        raise ValueError("edit receipt has no landed dest to announce")
    files = receipt.get("files") or []
    payload = json.dumps({
        "source": receipt.get("source"),
        "dest": dest,
        "sha256": files[0].get("sha256") if files else None,
    }, sort_keys=True)
    return send_to(target_uid, target_service, EDIT_READY_KIND, payload,
                   timeout=timeout)


# ---- workflow-runner consumer (07-disposables-plan §Lifecycle "workflow step
# completed") --------------------------------------------------------------
#
# The disposable teardown surface is SHIPPED + VM-proven: the admin-gated
# ``org.qdistro.SessionManager1.DisposeByWorkflow(s)->i`` reaps every disposable
# carrying the shared ``qdistro_lease_workflow=<id>`` podman label (stamped at
# spawn when ``QDISTRO_DISPOSABLE_WORKFLOW=<id>`` is set). This is the missing
# CONSUMER: a thin runner that (a) generates/accepts a workflow id, (b) opens
# step disposables tagged into that group (reusing ``open_in_disposable`` /
# ``open_for_edit`` — never a second spawn path), and (c) on step completion OR
# context exit (including an exception unwind) calls DisposeByWorkflow so the
# group is torn down. The daemon stays the security boundary: it validates /
# audits / fail-closes, so this layer is convenience + lifecycle glue only.


def _validate_workflow_id(workflow_id: object) -> str:
    """Return ``workflow_id`` iff it is a well-formed workflow lease id, else
    raise ``ValueError``. The SDK validates client-side because spawn-tier2.sh
    *silently ignores* an invalid ``QDISTRO_DISPOSABLE_WORKFLOW``: an invalid id
    would spawn an UNTAGGED disposable that no DisposeByWorkflow can reap — a
    silent leak the daemon cannot catch. Kept byte-identical to the daemon's
    ``is_workflow_id`` regex so the SDK never stamps an id the daemon rejects."""
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        raise ValueError(
            f"invalid workflow id {workflow_id!r}: want "
            r"^[a-z0-9][a-z0-9-]{0,127}$")
    return workflow_id


def generate_workflow_id(prefix: str = "wf") -> str:
    """Mint a fresh, regex-valid workflow id ``<prefix>-<16 hex>``.

    ``prefix`` is normalised/validated so the whole id satisfies the daemon's
    128-char lowercase-token shape; a 16-hex-char random suffix makes collisions
    between concurrent runs negligible. Raises ``ValueError`` if ``prefix`` is
    not a valid leading token or the result would exceed the length cap."""
    token = secrets.token_hex(8)  # 16 hex chars
    p = str(prefix)
    # The prefix itself must be a valid id head; the common caller mistakes
    # (empty / uppercase / leading-dash / illegal byte / overlong) are caught
    # here rather than producing a malformed id that silently fails to tag. A
    # trailing dash IS accepted by the regex (it's a label value, not a name), so
    # ``wf-`` -> ``wf--<token>`` is intentionally valid.
    if not _WORKFLOW_ID_RE.fullmatch(p):
        raise ValueError(
            f"invalid workflow id prefix {prefix!r}: want "
            r"^[a-z0-9][a-z0-9-]{0,127}$")
    wid = f"{p}-{token}"
    return _validate_workflow_id(wid)


def _session_bus():
    """Return the system bus the SessionManager1 surface lives on. A seam so
    unit tests can monkeypatch a fake bus without a real system D-Bus."""
    return dbus.SystemBus()


def dispose_workflow(workflow_id: str, *, bus=None,
                     timeout: float = DEFAULT_TIMEOUT_S) -> int:
    """Tear down EVERY disposable in the ``workflow_id`` group by calling
    ``org.qdistro.SessionManager1.DisposeByWorkflow`` on the SYSTEM bus. Returns
    the count torn down (0 idempotently when none carry the id).

    This is the EXPLICIT teardown surface: it PROPAGATES errors (a malformed id
    is a client-side ``ValueError`` before any wire call; a daemon refusal /
    fail-closed BadState surfaces as a ``dbus.DBusException``) because a caller
    that asked for teardown should learn it did not complete. The context-manager
    exit paths (:class:`WorkflowRun` / step) deliberately SWALLOW teardown errors
    instead, so a best-effort cleanup never masks an in-flight user exception.

    The daemon is the boundary: it admin-gates, validates the id again, audits,
    and does not report clean success on a partial teardown — so this helper adds
    no second policy copy, only the wire call + client-side fail-fast."""
    _validate_workflow_id(workflow_id)
    conn = bus if bus is not None else _session_bus()
    obj = conn.get_object(_SM_BUS_NAME, _SM_OBJ_PATH)
    iface = dbus.Interface(obj, _SM_IFACE)
    return int(iface.DisposeByWorkflow(str(workflow_id), timeout=float(timeout)))


class _WorkflowStep:
    """A per-step disposable subgroup with its OWN workflow id. Opening a
    disposable via the step tags it into the step's group, so the step's exit
    tears down only what that step spawned. A container carries exactly ONE
    ``qdistro_lease_workflow`` label, so a step disposable carries the STEP id
    (not the parent's) — the parent's exit therefore sweeps every step id it
    minted (see :class:`WorkflowRun`)."""

    def __init__(self, run: WorkflowRun, step_id: str):
        self._run = run
        self._id = _validate_workflow_id(step_id)
        self._disposed = False

    @property
    def id(self) -> str:
        return self._id

    def open_in_disposable(self, path: str, *, class_name: str, **kw) -> dict:
        return _open_tagged(self._id, path, class_name=class_name, **kw)

    def open_for_edit(self, path: str, *, class_name: str, request_silo: str,
                      **kw) -> dict:
        return _open_tagged(self._id, path, class_name=class_name,
                            request_silo=request_silo, _edit=True, **kw)

    def dispose(self, *, timeout: float = DEFAULT_TIMEOUT_S) -> int:
        """Explicit early teardown of this step's group. Propagates errors."""
        n = dispose_workflow(self._id, bus=self._run._bus, timeout=timeout)
        self._disposed = True
        return n

    def __enter__(self) -> _WorkflowStep:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Best-effort: tear down this step's group, swallowing teardown errors so
        # a cleanup failure never masks an in-flight user exception. The parent
        # run's final sweep is idempotent, so a swallowed failure here is retried
        # by the parent (and the daemon already audits every attempt).
        self._run._teardown_quietly(self._id)
        self._disposed = True
        return False  # never suppress the user's exception


class WorkflowRun:
    """Groups the disposables a workflow (and its steps) spawn under workflow
    ids, and guarantees teardown on context exit — including an exception unwind.

    Minimal, ergonomic use::

        with WorkflowRun() as wf:                 # mints a fresh id
            wf.open_in_disposable(path, class_name="agent-scratch")
            with wf.step() as st:                 # a distinct sub-group
                st.open_in_disposable(other, class_name="agent-scratch")
            # st exit -> DisposeByWorkflow(st.id)
        # wf exit -> DisposeByWorkflow for wf.id AND every step id it minted

    Disposables opened directly on the run carry ``wf.id``; those opened on a
    step carry the step's distinct id (one label per container). On exit the run
    tears down its own group AND every step id it created (idempotent — a step
    that already exited is a 0-count re-call), so a step that raised mid-launch,
    or a disposable whose spawn errored AFTER the container was created, is still
    reaped by the by-label group teardown the daemon performs. Exit swallows
    teardown errors so cleanup never masks the user's exception; the explicit
    :meth:`dispose` / step ``dispose`` surfaces PROPAGATE errors."""

    def __init__(self, workflow_id: str | None = None, *, bus=None):
        self._id = (generate_workflow_id() if workflow_id is None
                    else _validate_workflow_id(workflow_id))
        self._bus = bus
        # Every step id we minted (so exit can sweep each — step containers carry
        # the step id, not wf.id). Insertion-ordered; values never popped so a
        # double-dispose is just an idempotent re-call.
        self._step_ids: list[str] = []
        self._step_n = 0

    @property
    def id(self) -> str:
        return self._id

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(self._step_ids)

    def open_in_disposable(self, path: str, *, class_name: str, **kw) -> dict:
        return _open_tagged(self._id, path, class_name=class_name, **kw)

    def open_for_edit(self, path: str, *, class_name: str, request_silo: str,
                      **kw) -> dict:
        return _open_tagged(self._id, path, class_name=class_name,
                            request_silo=request_silo, _edit=True, **kw)

    def step(self, name: str | None = None) -> _WorkflowStep:
        """Begin a step: a distinct sub-group with its own workflow id. ``name``
        (if given) is woven into the id for readable audit lines; otherwise a
        running counter is used. The id is length-capped to satisfy the daemon's
        128-char regex (the parent prefix is truncated if needed so the step
        suffix always fits)."""
        self._step_n += 1
        suffix = f"s{self._step_n}"
        if name:
            n = re.sub(r"[^a-z0-9-]", "-", str(name).lower()).strip("-")
            if n:
                suffix = f"{suffix}-{n}"
        # Cap the whole id at 128 chars: trim the parent-id head, never the
        # suffix (which carries the step identity). +1 for the joining dash.
        head_max = 128 - len(suffix) - 1
        if head_max < 1:
            # Pathological: suffix alone (from a huge name) would blow the cap —
            # fall back to a fresh standalone id rather than a malformed one.
            step_id = generate_workflow_id("wfstep")
        else:
            head = self._id[:head_max].rstrip("-") or self._id[:1]
            step_id = _validate_workflow_id(f"{head}-{suffix}")
        self._step_ids.append(step_id)
        return _WorkflowStep(self, step_id)

    def dispose(self, *, timeout: float = DEFAULT_TIMEOUT_S) -> int:
        """Explicitly tear down the run's OWN group (not the step subgroups —
        use the step's ``dispose`` or let exit sweep them). Propagates errors."""
        return dispose_workflow(self._id, bus=self._bus, timeout=timeout)

    def _teardown_quietly(self, workflow_id: str) -> None:
        """Tear down one group, swallowing+logging any error (the cleanup-path
        contract: never raise, never mask an in-flight exception). Uses the short
        cleanup-path timeout so a wedged daemon can't block exit for the full
        600s default per group."""
        try:
            dispose_workflow(workflow_id, bus=self._bus,
                             timeout=CLEANUP_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            log.warning("workflow teardown of %r failed (swallowed): %s",
                        workflow_id, e)

    def __enter__(self) -> WorkflowRun:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Sweep every step group we minted, then the run's own group. All
        # by-label + idempotent, so a step that already exited cleanly is a
        # 0-count re-call and a spawn-that-errored-mid-launch is still reaped.
        # Swallow each so cleanup never masks the user's exception.
        for sid in self._step_ids:
            self._teardown_quietly(sid)
        self._teardown_quietly(self._id)
        return False  # never suppress the user's exception


def _open_tagged(workflow_id: str, path: str, *, class_name: str,
                 request_silo: str | None = None, _edit: bool = False,
                 extra_env: dict[str, str] | None = None,
                 **kw) -> dict:
    """Open a disposable tagged into ``workflow_id`` by injecting the
    ``QDISTRO_DISPOSABLE_WORKFLOW`` env var, then delegating to the existing
    spawn helper (NO second spawn path). The runner OWNS the tag: a caller's
    ``extra_env`` is merged FIRST, then the workflow var is set unconditionally,
    so a caller cannot override (or accidentally clear) the tracked id and strand
    an untagged — unreapable — disposable."""
    _validate_workflow_id(workflow_id)
    env = dict(extra_env) if extra_env else {}
    # Runner-owned: stamp last so it always wins over any caller value.
    env[WORKFLOW_ENV] = workflow_id
    if _edit:
        if not request_silo:
            raise OpenInDisposableError("request_silo must be a non-empty string")
        return open_for_edit(path, class_name=class_name,
                             request_silo=request_silo, extra_env=env, **kw)
    return open_in_disposable(path, class_name=class_name, extra_env=env, **kw)


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
