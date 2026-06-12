"""Disposable-silo helpers (07-disposables-plan P1).

A disposable is a tier-2 silo *flavor*, not a new resource kind: a
`podman run --rm` container with a tmpfs home and no state dir, so discard is
by construction. These pure helpers carry the naming, secctx app_id, and the
reaper-sweep decision so they have a single source of truth and can be
unit-tested without podman / a live session manager.

Identifiers (D15):
- container name: ``disp-<workload>-<YYYYMMDD-HHMMSS>`` (timestamp at end,
  human-orderable; a short random suffix only on a same-second collision).
- secctx app_id: ``qdistro.disp.<token>`` (a per-launch random hex token; the
  sandbox_engine stays ``qdistro.tier2`` — it is still a tier-2 container).

Reaper: a disposable must never outlive its session. Every ``disp-*``
container is therefore a sweep target at session stop/boot — there is no
legitimate disposable that should survive those boundaries (a still-running
one is a crash/leak). See qdistro_session_manager.reap_disposable_containers.
"""
from __future__ import annotations

import re

DISP_PREFIX = "disp-"
SECCTX_APPID_PREFIX = "qdistro.disp."

# A workload label becomes part of a container name and a broker action, so
# constrain it to a safe, lowercase, DNS-ish token.
_WORKLOAD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Timestamp component YYYYMMDD-HHMMSS, optional -<hexsuffix> for collisions.
_NAME_RE = re.compile(
    r"^disp-(?P<workload>[a-z0-9][a-z0-9-]*?)-"
    r"(?P<ts>\d{8}-\d{6})(?:-(?P<suffix>[0-9a-f]{1,8}))?$")
# secctx token: random hex.
_TOKEN_RE = re.compile(r"^[0-9a-f]{8,64}$")


class DisposableError(ValueError):
    """Invalid disposable identifier."""


def validate_workload(workload: str) -> str:
    if not isinstance(workload, str) or not _WORKLOAD_RE.fullmatch(workload):
        raise DisposableError(
            f"invalid disposable workload {workload!r}: must match "
            f"{_WORKLOAD_RE.pattern}")
    return workload


def disposable_name(workload: str, ts: str, suffix: str = "") -> str:
    """Build a disposable container name. ``ts`` is a YYYYMMDD-HHMMSS string
    (the caller supplies it — this module takes no clock so it stays pure and
    testable). ``suffix`` is an optional short hex collision breaker."""
    validate_workload(workload)
    if not re.fullmatch(r"\d{8}-\d{6}", ts):
        raise DisposableError(f"invalid timestamp {ts!r} (want YYYYMMDD-HHMMSS)")
    name = f"{DISP_PREFIX}{workload}-{ts}"
    if suffix:
        if not re.fullmatch(r"[0-9a-f]{1,8}", suffix):
            raise DisposableError(f"invalid collision suffix {suffix!r}")
        name = f"{name}-{suffix}"
    return name


def is_disposable_container(name: str) -> bool:
    """True iff ``name`` is a well-formed disposable container name. Used by
    the reaper — a sloppy ``startswith('disp-')`` would also match an
    admin-created container that merely happens to start with ``disp-``;
    require the full shape so the sweep only ever targets our own."""
    return bool(isinstance(name, str) and _NAME_RE.fullmatch(name))


def parse_disposable_name(name: str) -> tuple[str, str] | None:
    """-> (workload, timestamp) or None if not a disposable name."""
    m = _NAME_RE.fullmatch(name or "")
    if not m:
        return None
    return m.group("workload"), m.group("ts")


def disposable_secctx_appid(token: str) -> str:
    """``qdistro.disp.<token>`` — the secctx app_id for a disposable launch."""
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise DisposableError(
            f"invalid disposable token {token!r}: want {_TOKEN_RE.pattern}")
    return f"{SECCTX_APPID_PREFIX}{token}"


def is_disposable_appid(app_id: str) -> bool:
    if not isinstance(app_id, str) or not app_id.startswith(SECCTX_APPID_PREFIX):
        return False
    return bool(_TOKEN_RE.fullmatch(app_id[len(SECCTX_APPID_PREFIX):]))


def is_disposable_token(token: str) -> bool:
    """True iff ``token`` is a well-formed per-spawn launch token (the random
    hex in ``qdistro_tier2_token`` / qdwin ``instanceId``). The dispose-by-token
    teardown surface validates the caller-supplied token with this BEFORE it
    ever reaches a podman label filter, so a malformed/oversized/injection-ish
    value is rejected at the door (fail-closed), never passed downstream."""
    return bool(isinstance(token, str) and _TOKEN_RE.fullmatch(token))


def dispose_action(workload: str) -> str:
    """The broker spawn-gate action for a disposable workload."""
    validate_workload(workload)
    return f"qdistro.dispose.spawn:{workload}"


def disp_sweep_targets(container_names: list[str]) -> list[str]:
    """The disposable containers to reap from a list of existing container
    names. Every well-formed ``disp-*`` is a target at a session boundary —
    nothing else is touched (non-disposable names are ignored, so an
    admin-named container is never collateral)."""
    return [n for n in container_names if is_disposable_container(n)]
