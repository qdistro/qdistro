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

# Process-tree-empty lease (07-disposables-plan §Lifecycle "last toplevel closed
# AND process tree empty"). An OPT-IN immutable marker label: when present the
# in-session sweep reaps a disposable whose inner process tree has collapsed to
# the compositor PID1 alone (no remaining client/helper/workload). HONESTY: with
# `podman run --rm`, PID1 (the nested weston) exit already auto-removes the
# container, so the leak this targets is "weston lingers as an empty compositor
# after every inner client exited" — NOT a true OS-level empty process tree
# (PID1 is by definition always present). The age check reuses LEASE_CREATED so a
# mid-startup pod (weston up, client not yet launched) is never reaped. Off by
# default: an interactive disposable left with an empty compositor is legitimate
# and must NOT acquire a surprise process-count kill — only windowless/agent/
# workflow pods that contract "no new work after the tree empties" opt in.
LEASE_PROCTREE_LABEL = "qdistro_lease_proctree"
LEASE_PROCTREE_GRACE_LABEL = "qdistro_lease_proctree_grace"
# Workflow-step lease (07-disposables-plan §Lifecycle "workflow step completed").
# An OPT-IN grouping label keyed on a workflow id: every disposable a workflow
# step spawns carries the same id so the step's completion can tear ALL of them
# down with one DisposeByWorkflow(id) call (the runner may have spawned several,
# or lost the per-spawn tokens across a restart). This is an EXTERNAL completion
# event, not a wall-clock condition — so it is a D-Bus teardown surface, never a
# periodic sweep predicate.
LEASE_WORKFLOW_LABEL = "qdistro_lease_workflow"

# The compositor that is always a disposable's PID1 (spawn-tier2 WRAPPER_BODY
# launches a nested weston as the container's init). The process-tree-empty
# predicate fires ONLY when this is the sole remaining process — no allowlist of
# weston helpers (their presence is weston-version/config dependent, so an
# allowlist could mis-read a missing helper as "empty"; PID1-only is unambiguous
# and has zero false-positive surface from unknown helper/client processes).
PROCTREE_PID1_COMM = "weston"
# Default grace seconds before a PID1-only disposable is eligible (covers the
# normal "weston is up before the inner client appears" startup race).
PROCTREE_GRACE_DEFAULT = 30

# A workload label becomes part of a container name and a broker action, so
# constrain it to a safe, lowercase, DNS-ish token.
_WORKLOAD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# A workflow id rides in a podman label and a podman --filter value; constrain it
# to the same conservative shape as a workload (no arbitrary label bytes reach a
# filter). It is never part of a container NAME, only a label, so the length cap
# is generous but the alphabet stays safe.
_WORKFLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
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


# ---------------------------------------------------------------------------
# Process-tree-empty lease helpers — pure, no clock, no podman
# ---------------------------------------------------------------------------


def lease_opt_in(raw: object) -> bool:
    """True iff a boolean opt-in label (``qdistro_lease_proctree``) reads exactly
    ``"1"``. Fail-closed: an absent label (podman's ``<no value>``), an empty
    string, ``"0"``, or anything else is NOT opted in, so the predicate is
    skipped (never reaped on a guess). Whitespace is tolerated for parity with
    ``parse_lease_seconds``."""
    if isinstance(raw, bool):  # bool is an int subclass — be explicit
        return raw is True
    if isinstance(raw, int):
        return raw == 1
    if not isinstance(raw, str):
        return False
    return raw.strip() == "1"


def parse_podman_top_pids(top_output: object) -> list[tuple[int, str]] | None:
    """Parse the output of ``podman top <ctr> pid comm`` into ``[(pid, comm)]``,
    or ``None`` if the output is unusable (=> the caller SKIPS — fail-closed).

    podman top prints a header row (``PID   COMMAND``) then one row per process.
    We require:
    - a non-empty output with at least the header + one process row,
    - every data row to be exactly two whitespace-separated fields whose first is
      a base-10 integer (the comm may itself contain no spaces for the ``comm``
      descriptor; we therefore split on the FIRST run of whitespace only).

    Returns ``None`` on: empty/blank output, a header-only table (no processes —
    a healthy container always shows at least PID1, so zero rows means podman
    gave us nothing trustworthy), or any row whose PID field is not a clean
    non-negative integer. This deliberately never *infers* emptiness from missing
    or malformed output — that path is a SKIP, not a reap."""
    if not isinstance(top_output, str):
        return None
    lines = [ln for ln in top_output.splitlines() if ln.strip()]
    if len(lines) < 2:
        # Need the header plus at least one process row. A healthy container
        # always has PID1; fewer rows means podman emitted nothing usable.
        return None
    rows: list[tuple[int, str]] = []
    for ln in lines[1:]:  # skip the header
        parts = ln.split(None, 1)
        if len(parts) != 2:
            return None
        pid_s, comm = parts[0], parts[1].strip()
        if not pid_s.isdigit():
            return None
        try:
            pid = int(pid_s)
        except ValueError:
            return None
        if not comm:
            return None
        rows.append((pid, comm))
    if not rows:
        return None
    return rows


def proctree_empty(top_output: object,
                   pid1_comm: str = PROCTREE_PID1_COMM) -> bool:
    """True iff a disposable's inner process tree has collapsed to the compositor
    PID1 alone — the *only* remaining process is PID 1 and its command BASENAME is
    ``pid1_comm`` (``weston``). No allowlist of weston helpers: their presence is
    weston-version/config dependent, so an allowlist could mis-read a missing
    helper as "empty"; requiring PID1-ONLY is unambiguous and has zero
    false-positive surface from any unknown helper/client/workload process.

    The PID1 command is matched on its BASENAME (``/usr/bin/weston`` and a bare
    ``weston`` both match): ``podman top <ctr> comm`` normally renders the bare
    command name, but matching the basename is robust to a path-form rendering
    without weakening the guard (only PID 1 is ever compared, and an inner client
    never runs as PID 1). It is NOT a substring/prefix match — ``weston-foo``
    would NOT match ``weston``.

    Fail-closed (returns ``False`` => the candidate is NOT reaped) on: unparseable
    output (``parse_podman_top_pids`` ``None``), MORE than one process row,
    multiple/zero PID-1 rows, or a PID1 command whose basename is not exactly
    ``pid1_comm``. Honesty: a ``True`` here means "only the compositor PID1
    remains; no client/helper/workload process is visible", NOT a true OS-level
    empty tree."""
    rows = parse_podman_top_pids(top_output)
    if rows is None:
        return False
    if len(rows) != 1:
        return False
    pid, comm = rows[0]
    if pid != 1:
        return False
    # Compare on the command basename so a path-form ``/usr/bin/weston`` and a
    # bare ``weston`` both match; an inner client never runs AS pid 1, so this is
    # only ever the nested compositor itself.
    return comm.rsplit("/", 1)[-1] == pid1_comm


def proctree_grace_elapsed(now_epoch: float, created_epoch: int | None,
                           grace_seconds: int | None) -> bool:
    """True iff a disposable is past its process-tree grace window at
    ``now_epoch`` — old enough that "only weston remains" is a settled state, not
    a mid-startup race where the inner client has simply not launched yet.

    Fail-SAFE: an unparseable/absent ``created_epoch`` (``None``) -> NOT elapsed
    (we cannot judge age, so we never reap on a guess). ``grace_seconds`` ``None``
    falls back to ``PROCTREE_GRACE_DEFAULT``; a negative grace is treated as the
    default (never as "no grace"). A negative age (the wall clock jumped
    backwards since spawn) clamps to NOT elapsed."""
    if created_epoch is None:
        return False
    if grace_seconds is None or grace_seconds < 0:
        grace_seconds = PROCTREE_GRACE_DEFAULT
    age = now_epoch - created_epoch
    if age < 0:
        return False
    return age >= grace_seconds


def proctree_candidate_eligible(candidate: dict, now_epoch: float) -> bool:
    """True iff a candidate is eligible for a process-tree-empty reap, on the
    metadata alone (the actual ``podman top`` emptiness check is a separate, more
    expensive step the store does only for eligible candidates).

    Mirrors ``lease_sweep_targets``' gate set: a well-formed ``disp-*`` name, a
    well-formed per-spawn token label, the ``qdistro_lease_proctree=1`` opt-in,
    and a settled grace window (parseable created instant, age past grace).
    Anything failing a guard is INELIGIBLE — never inspected, never reaped. Pure:
    no I/O, no clock (``now_epoch`` is injected)."""
    if not is_disposable_container(candidate.get("name")):
        return False
    if not is_disposable_token(candidate.get("token") or ""):
        return False
    if not lease_opt_in(candidate.get("proctree")):
        return False
    created = parse_lease_seconds(candidate.get("created"))
    grace = parse_lease_seconds(candidate.get("grace"))
    return proctree_grace_elapsed(now_epoch, created, grace)


def is_workflow_id(workflow_id: object) -> bool:
    """True iff ``workflow_id`` is a well-formed workflow lease id (the value of
    ``qdistro_lease_workflow`` / the ``DisposeByWorkflow`` argument). Validated
    BEFORE it ever reaches a podman ``--filter`` value so a malformed/oversized/
    injection-ish id is rejected at the door (fail-closed), never passed
    downstream. Same conservative lowercase token shape as a workload."""
    return bool(isinstance(workflow_id, str)
                and _WORKFLOW_ID_RE.fullmatch(workflow_id))
