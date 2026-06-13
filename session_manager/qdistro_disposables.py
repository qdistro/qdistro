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

# Lease (07-disposables-plan §Lifecycle): a disposable may carry a max-lifetime
# TTL so a windowless/background leak (a helper that outlived its driver, an
# agent pod whose workflow crashed before calling dispose) is reaped in-session
# rather than only at the next boot/logout boundary. Both values are authored
# at spawn time as immutable integer podman labels (NOT read from podman's
# version-volatile created-time field): the TTL in seconds, and the creation
# instant as a unix-epoch second. A disposable with no TTL label (or TTL 0) has
# NO lease and is never reaped by the sweep — interactive disposables opt out by
# default and rely on window-close + --rm, exactly as before.
LEASE_TTL_LABEL = "qdistro_lease_ttl"
LEASE_CREATED_LABEL = "qdistro_lease_created"

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


# ---------------------------------------------------------------------------
# Lease (TTL max-lifetime) helpers — pure, no clock, no podman
# ---------------------------------------------------------------------------


def parse_lease_seconds(raw: object) -> int | None:
    """Parse a lease label value (``qdistro_lease_ttl`` / ``qdistro_lease_created``
    as podman emits it) to a non-negative ``int`` of seconds, or ``None`` if it
    is absent or malformed.

    Fail-closed: anything that is not a clean non-negative base-10 integer
    returns ``None`` so the candidate is SKIPPED (never reaped on a guess).
    This rejects a missing label (podman renders an absent label as the literal
    ``<no value>``), the empty string, a sign, a float, embedded whitespace,
    and non-digit garbage. Python ``int`` has no overflow, so a huge but
    well-formed value parses fine (and simply never expires in practice)."""
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # str.isdigit() is True only for a non-empty run of decimal digits: it
    # rejects '', '<no value>', '-5', '5.0', '5 6', '0x5', and unicode oddities
    # (isdigit allows some superscripts, so guard with the ASCII int parse).
    if not s.isdigit():
        return None
    try:
        v = int(s)
    except ValueError:
        return None
    return v if v >= 0 else None


def lease_expired(now_epoch: float, created_epoch: int | None,
                  ttl_seconds: int | None) -> bool:
    """True iff a disposable's TTL lease has expired at ``now_epoch``.

    No lease / opt-out: ``ttl_seconds`` ``None`` or ``<= 0`` -> never expired.
    Fail-safe: an unparseable/absent created instant (``None``) -> never
    expired (we cannot judge age, so we never reap on a guess). A negative age
    (the wall clock jumped backwards since spawn) clamps to not-expired rather
    than reaping early."""
    if ttl_seconds is None or ttl_seconds <= 0:
        return False
    if created_epoch is None:
        return False
    age = now_epoch - created_epoch
    if age < 0:
        return False
    return age > ttl_seconds


def lease_sweep_targets(candidates: list[dict], now_epoch: float) -> list[str]:
    """Names of disposables whose TTL lease has expired at ``now_epoch``.

    Each candidate is a raw dict ``{name, token, ttl, created}`` with the
    strings podman emitted (or ``None``). A candidate is eligible for reaping
    ONLY when every guard passes: a well-formed ``disp-*`` name, a well-formed
    per-spawn token label (so an accidental/baked ``qdistro_disposable=1`` on a
    container that is not one of our spawned disposables is excluded), a
    parseable TTL and created instant, and an elapsed lease. Anything that
    fails a guard is SKIPPED — the sweep never reaps on a guess, and final
    removal is still re-validated by ``dispose()``. Pure: no I/O, no clock."""
    targets: list[str] = []
    for c in candidates:
        name = c.get("name")
        if not is_disposable_container(name):
            continue
        if not is_disposable_token(c.get("token") or ""):
            continue
        ttl = parse_lease_seconds(c.get("ttl"))
        created = parse_lease_seconds(c.get("created"))
        if lease_expired(now_epoch, created, ttl):
            targets.append(name)
    return targets
