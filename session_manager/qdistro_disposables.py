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

import json
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


# ---------------------------------------------------------------------------
# Robust ``podman ps --format json`` row parsing — pure, fail-closed
# ---------------------------------------------------------------------------
#
# The sweep candidate enumerations read ``podman ps -a --format json`` rather
# than a Go-template line joined by a separator. ``--format json`` emits a
# single JSON array whose every label value is JSON-ESCAPED (embedded newlines,
# NUL, control chars, quotes, tabs, unicode, and the old US separator are all
# encoded), so an attacker-controlled label value can NOT inject a fake field or
# spill into a forged record the way a literal newline could under a
# ``splitlines()`` + ``split(sep)`` parse. ``json.loads`` is the single trust
# boundary; ALL semantic validation (name shape, hex token, lease ints, opt-in)
# still happens in the helpers above, so a garbled-but-well-escaped value maps to
# a SKIP exactly as before.


def parse_podman_ps_json(raw: object, label_keys: list[str]) -> list[dict]:
    """Parse ``podman ps -a --format json`` output into one normalized
    ``{"name": <str>, <label_key>: <str|None>, ...}`` dict per usable record.

    ``label_keys`` are the podman label names to pull (e.g.
    ``["qdistro_tier2_token", "qdistro_lease_ttl"]``); each is exposed verbatim
    in the result dict (the caller re-maps to its candidate key names). A label
    that is absent / null / not a string becomes ``None`` (rendered downstream by
    the existing fail-closed parsers as "skip").

    Fail-closed:
    - ``json.loads`` failure, a non-``str``/``bytes`` input, or a top-level value
      that is not a list -> ``[]`` (the whole pass yields no candidates; the
      caller treats this like a podman failure and the boundary reaper backstops).
    - A single malformed RECORD (not a dict, no usable ``Names``) is SKIPPED — it
      does NOT poison the rest of the array.

    Name extraction tolerates both the modern shape (``Names`` is a non-empty
    list of strings — the first entry is the canonical name) and a defensive
    fallback (``Names`` is a plain string, or a singular ``Name`` string) for
    podman-version tolerance. A record whose name is not a non-empty string is
    skipped (no row without a name ever reaches the name-shape gate).

    The returned ``name`` is whatever podman reported (RAW, unvalidated here):
    ``is_disposable_container`` downstream is the authority on whether it is one
    of ours. This keeps a forged ``Names`` value from being silently "accepted"
    — it just flows to the same gate every name flows to."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return []
    if not isinstance(raw, str):
        return []
    s = raw.strip()
    if not s:
        return []
    try:
        data = json.loads(s)
    except (ValueError, TypeError):
        # Malformed/garbled JSON: fail closed for the whole pass. A truncated or
        # injected stream must never be parsed "best-effort" into partial rows.
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for rec in data:
        if not isinstance(rec, dict):
            continue  # skip a malformed record; never poison the array
        name = _ps_record_name(rec)
        if name is None:
            continue
        labels = rec.get("Labels")
        if not isinstance(labels, dict):
            labels = {}
        row: dict = {"name": name}
        for key in label_keys:
            v = labels.get(key)
            # Only a genuine string label value passes through; anything else
            # (None/null, int, nested object, a non-string key collision) becomes
            # None so the downstream int/opt-in/token parsers SKIP it.
            row[key] = v if isinstance(v, str) else None
        out.append(row)
    return out


def _ps_record_name(rec: dict) -> str | None:
    """Extract the canonical container name from a ``podman ps --format json``
    record, or ``None`` if there is no usable name. Modern podman renders
    ``Names`` as a non-empty list of strings; a defensive fallback accepts a
    plain-string ``Names`` or a singular ``Name``. A non-string / empty name is
    rejected (``None``) so no nameless row reaches the name-shape gate."""
    names = rec.get("Names")
    if isinstance(names, list):
        for n in names:
            if isinstance(n, str) and n:
                return n
        return None
    if isinstance(names, str) and names:
        return names
    name = rec.get("Name")
    if isinstance(name, str) and name:
        return name
    return None


# ---------------------------------------------------------------------------
# Stuck-podman-descendant cleanup — pure host-PID kill-candidate decision
# ---------------------------------------------------------------------------
#
# After a ``podman rm -f <disp>`` TIMES OUT (the M1 wedge guard returns False),
# the container may still be alive with a stuck descendant tree (a hung
# container child, conmon, pasta, slirp4netns) holding resources, so the next
# sweep re-wedges the same way and the resources leak. The cleanup SIGKILLs the
# container's PAYLOAD host PIDs — but ONLY host PIDs that are provably this
# container's, verified via the cgroup membership of each pid. These pure helpers
# carry the safety decision (which host pids are killable given the full
# container id + each pid's /proc/<pid>/cgroup text) so it is unit-fuzzable with
# hostile inputs and never touches a host process that is not this container's.
#
# Critical invariants:
# - Operate on HOST pids only (``podman top hpid``), NEVER container-namespace
#   pids (``podman top pid``): killing a container-ns pid host-side would target
#   an UNRELATED host process.
# - A pid is killable ONLY if its cgroup text contains the FULL 64-hex container
#   id as a distinct ``libpod-<id>.scope`` path component (not a loose
#   substring) — an exact-component match has effectively no accidental
#   false-positive surface.
# - Ambiguity (unreadable/blank cgroup, no exact-component match, a malformed id,
#   a non-positive pid) -> NOT killable (skip). Fail-safe: never kill on a guess.

# A podman/libpod container id is a 64-char lowercase hex string.
_FULL_CTR_ID_RE = re.compile(r"^[0-9a-f]{64}$")


def is_full_container_id(cid: object) -> bool:
    """True iff ``cid`` is a well-formed full (64 hex) podman container id. The
    cgroup-membership check requires the FULL id (a short/truncated id could
    appear by chance and weaken the exact-component match), so this gates the id
    before it is ever used to authorize a kill."""
    return bool(isinstance(cid, str) and _FULL_CTR_ID_RE.fullmatch(cid))


def parse_podman_top_hpids(top_output: object) -> list[int] | None:
    """Parse ``podman top <ctr> hpid comm`` output into a list of HOST pids, or
    ``None`` if the output is unusable (=> the caller does NO cleanup —
    fail-safe). ``hpid`` is podman's explicit HOST pid descriptor; these are the
    only pids it is ever safe to ``os.kill`` from the host.

    podman top prints a header (``HPID  COMMAND``) then one row per process. We
    require a non-empty table; every DATA row's first whitespace-field must be a
    clean positive base-10 integer (pid 0 / negatives are rejected — never a
    kill target). A header-only table yields ``[]`` (no payload pids visible — a
    legitimate "nothing to clean up"). Any malformed row -> ``None`` (refuse the
    whole cleanup rather than kill a partial, possibly-misparsed set)."""
    if not isinstance(top_output, str):
        return None
    lines = [ln for ln in top_output.splitlines() if ln.strip()]
    if not lines:
        return None
    pids: list[int] = []
    for ln in lines[1:]:  # skip the header row
        parts = ln.split(None, 1)
        if not parts:
            return None
        pid_s = parts[0]
        if not pid_s.isdigit():
            return None
        try:
            pid = int(pid_s)
        except ValueError:
            return None
        if pid <= 0:  # pid 0 is the kernel/"every process"; never a target
            return None
        pids.append(pid)
    return pids


def cgroup_belongs_to_container(cgroup_text: object, full_id: object) -> bool:
    """True iff the ``/proc/<pid>/cgroup`` text proves the pid lives inside the
    container whose full id is ``full_id`` — i.e. some cgroup path contains the
    exact component ``libpod-<full_id>.scope``. This is the authorization for a
    SIGKILL, so it is deliberately strict:

    - ``full_id`` must be a well-formed full container id (else -> False).
    - The match is on a distinct PATH COMPONENT ``libpod-<id>.scope`` (split on
      ``/``), NOT a loose substring — a malicious delegated cgroup path that
      merely *embeds* the id as part of a longer name does not authorize a kill.
    - Unreadable/blank/non-str cgroup text -> False (skip; never kill on a guess).

    Conservative by design: a legitimate descendant whose cgroup placement does
    not carry the scope component (a podman-version/config quirk) is SKIPPED
    rather than killed — a missed straggler is retried next sweep, but an
    unrelated process is NEVER killed."""
    if not is_full_container_id(full_id):
        return False
    if not isinstance(cgroup_text, str):
        return False
    scope = f"libpod-{full_id}.scope"
    for ln in cgroup_text.splitlines():
        # A v2 line is "0::/path"; a v1 line is "N:ctrl:/path". The path is the
        # final colon-separated field. Match the scope as a whole '/'-component.
        path = ln.rsplit(":", 1)[-1]
        for comp in path.split("/"):
            if comp == scope:
                return True
    return False


def stuck_descendant_kill_set(
        hpids: object, cgroup_by_pid: dict, full_id: object) -> list[int]:
    """The host pids it is SAFE to SIGKILL to clean up a stuck disposable, given:
    ``hpids`` (host pids from ``parse_podman_top_hpids`` — ``None`` => no
    cleanup), ``cgroup_by_pid`` ({pid: /proc/<pid>/cgroup text or None if it
    could not be read}), and the container's ``full_id``.

    A pid is included ONLY if it is a positive int AND its recorded cgroup text
    proves container membership via ``cgroup_belongs_to_container``. Everything
    else is excluded (fail-safe). ``full_id`` not a full container id, or
    ``hpids`` ``None`` -> ``[]`` (refuse all cleanup — never kill on an
    unverifiable identity). Pure: no I/O, no ``os.kill``; the caller does the
    bounded reads and the kills."""
    if hpids is None or not is_full_container_id(full_id):
        return []
    if not isinstance(hpids, (list, tuple)):
        return []
    targets: list[int] = []
    for pid in hpids:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        cg = cgroup_by_pid.get(pid) if isinstance(cgroup_by_pid, dict) else None
        if cgroup_belongs_to_container(cg, full_id):
            targets.append(pid)
    return targets
