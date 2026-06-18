"""Production silo → security-snapshot resolver (the keystone the broker-central
lineage work blocked on).

``doc/design/broker-lineage-r1.md`` shipped broker-central STRUCTURAL lineage for
disposable exports but deliberately refused to call
:func:`qdistro_lineage.record_chokepoint`, because a chokepoint with an empty
:class:`~qdistro_guard_registry.FlowEndpoint` would mint a *clean-looking* guard
union for a source whose real classification is unknown. It marked every source/
output with a sealed ``security.snapshot.state = "unresolved"`` assertion instead,
and named the exact follow-on this module is:

    Before switching to record_chokepoint, implement a production source-security
    resolver with explicit authority:
      - inputs: request_silo / the verified silo;
      - outputs: FlowEndpoint(guards, compartments, conflict_classes) + state;
      - source of truth: a production policy record, NOT illustrative docs;
      - failure mode: unresolved, not empty-clean.

Authority model — the central snapshot store behind a ``SnapshotAuthority`` seam
=================================================================================

The authoritative source of a silo's ``{guards, compartments, conflict_classes}``
security snapshot is a **central control-plane store** (decision
``todo/decisions/silo-snapshot-authority-control-plane.md``, Jan, 2026-06-18):
modelled on Kubernetes, the end state is a control-plane *daemon* that owns a
centralized repository of silo/pod snapshots (desired + observed state), drives
silo updates through a reconcile loop with post-update health/readiness probes,
and is the prerequisite for cross-machine silo migration. That full daemon +
reconcile loop + migration is a **future, phased program — NOT built here**.

The **v1 slice** built here is the prerequisite for that daemon: a single
source-of-truth snapshot store, reached by the resolver through a narrow
:class:`SnapshotAuthority` seam, so a future daemon-owned store can be swapped in
WITHOUT touching the resolver's fail-closed semantics. The resolver depends only
on the seam (``snapshot_for_silo``), never on a concrete backing store, env var,
or raw registry dict.

The v1 BOOTSTRAP implementation of that seam is :class:`TomlSnapshotAuthority`,
backed by a strict, root-owned, fail-closed TOML file
(``/etc/qdistro/silo-security.toml``). This TOML is explicitly the *daemon's
bootstrap store*, NOT the design end state — a future control-plane daemon will
own/populate the central store (e.g. a structured store it can write) behind the
same seam. The TOML is *not* the illustrative ``doc/resources.md`` /
``doc/guards.md`` taxonomy (that taxonomy is explicitly non-final; loading it
would "look real while reading a non-final taxonomy"). The bootstrap file:

* lives in an operational config path, installed by packaging / edited by admin
  policy until the daemon owns it (the same trust class as
  ``/etc/qdistro/disposable-classes.toml`` and ``/etc/qdistro/rules.d``);
* is parsed by THIS broker code with a stable, strict, fail-closed schema —
  unknown keys reject, unknown guards reject (guard vocabulary is tied to
  :data:`~qdistro_metadata_schema.RESERVED_GUARDS`), malformed compartment/
  conflict slugs reject;
* is ownership/mode checked: it can mint a *resolved* ``FlowEndpoint``, so a
  group/world-writable or non-root-owned file is equivalent to letting the writer
  mint compartments and guards — that fails closed (:class:`RegistryError`).

The runtime authority CHAIN that yields a *resolved* snapshot is three links, and
ALL must hold (:func:`resolve_subject_silo_security`):

1. a live ``pid`` resolves through :func:`qdistro_resolver.resolve_subject` to a
   ``verified=True`` :class:`~qdistro_resolver.Subject` (a matching, unexpired
   launch record whose live kernel facts still agree — the anti-PID-reuse /
   anti-forgery anchor);
2. that verified subject's ``silo`` is non-empty (an unverified subject carries
   :data:`~qdistro_resolver.UNKNOWN_SILO` == ``""``, which can never match);
3. that silo name resolves to a profile through the :class:`SnapshotAuthority`
   (in v1, present in the bootstrap TOML store).

Any broken link → ``state="unresolved"`` with an EMPTY ``FlowEndpoint`` *placeholder*
(unknown, NOT clean). Crucially the resolver consumes ONLY the launcher-attested
``Subject.silo`` — never a caller-supplied ``request_silo`` string — so an attacker
who can name an arbitrary silo can only ever *fail* to resolve, never forge a
resolved (or wrong-compartment) snapshot.

Anti-laundering contract
=========================

``record_chokepoint`` records the endpoint's guard/compartment/conflict union
directly onto the derived entity. If a caller fed it the empty endpoint of an
*unresolved* source, the output would carry empty guards that downstream code
could mistake for clean — exactly the laundering path this module exists to close.
So the snapshot is misuse-hard:

* it carries an explicit :attr:`SiloSecuritySnapshot.state`, not just a
  ``FlowEndpoint``;
* :meth:`SiloSecuritySnapshot.require_resolved` raises :class:`UnresolvedSilo`
  unless the snapshot is resolved — a caller that wants to enter the chokepoint
  branch MUST go through it, so an unresolved empty endpoint cannot silently
  reach ``record_chokepoint``.

open_class is deferred for v1 (silo is the authority). A future open_class
refinement may only produce a MONOTONIC JOIN over the silo snapshot (e.g. force
``local-only`` for a hostile-input class); it must never remove a silo guard,
change compartments, or narrow conflict classes without a separate
authority-bearing declassification/transfer workflow.

Style mirrors ``qdistro_disposable_classes`` (strict tomllib loader, frozen
dataclasses, no third-party deps) and ``qdistro_resolver`` (fail-closed Subject
posture).
"""
from __future__ import annotations

import logging
import os
import re
import stat
import tomllib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("qdistro.silo_security")

from qdistro_guard_registry import FlowEndpoint
from qdistro_metadata_schema import RESERVED_GUARDS
from qdistro_resolver import UNKNOWN_SILO, resolve_subject

# v1 bootstrap store path. The broker reaches the central snapshot store through
# the SnapshotAuthority seam (default: TomlSnapshotAuthority over this file); a
# future control-plane daemon owns/replaces the backing store behind the same
# seam. Tests override the path via the QDISTRO_SILO_SECURITY env var.
REGISTRY_PATH_DEFAULT = "/etc/qdistro/silo-security.toml"
REGISTRY_PATH_ENV = "QDISTRO_SILO_SECURITY"

#: Snapshot states. ``resolved`` is the ONLY value a caller may treat as
#: authoritative enough to feed a real FlowEndpoint into record_chokepoint;
#: ``unresolved`` keeps the structural-only / security.snapshot.state="unresolved"
#: path (empty endpoint = unknown, not clean).
STATE_RESOLVED = "resolved"
STATE_UNRESOLVED = "unresolved"

#: Keys a ``[silo.<name>]`` table may carry. An unknown key is a typo
#: (``compartment`` vs ``compartments``, ``conflictClasses`` camelCase) that could
#: leave a real field at its empty default and silently under-classify a silo —
#: reject it (fail-closed), mirroring the disposable-classes loader.
_ALLOWED_KEYS = frozenset(("guards", "compartments", "conflict_classes"))

#: Silo name + compartment + conflict-class slugs: DNS-label-ish, lowercase,
#: bounded. Mirrors the conservative slug shape qdistro uses elsewhere (the doc
#: examples are ``work`` / ``home`` / ``home-work-separation``). A slug rides into
#: sealed lineage rows and policy comparisons, so it is constrained: lowercase
#: alnum start/end, alnum/dot/hyphen interior, no whitespace/control bytes, no
#: ``..`` traversal hygiene, bounded length.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")


class RegistryError(ValueError):
    """The silo-security registry file is malformed / unsafe / unloadable.

    Fail-closed: a caller that cannot load the registry resolves EVERY silo to
    ``unresolved`` rather than falling back to a permissive (clean) default. The
    file can mint resolved security snapshots, so an ownership/mode failure, an
    unknown key, an unknown guard, or a malformed slug all refuse the whole file.
    """


class UnresolvedSilo(PermissionError):
    """Raised by :meth:`SiloSecuritySnapshot.require_resolved` when a caller tries
    to use an unresolved snapshot as an authoritative FlowEndpoint. This is the
    misuse-hard guard that keeps an unresolved empty endpoint OUT of
    ``record_chokepoint`` (the laundering path this module closes)."""


def _valid_slug(value: object) -> bool:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        return False
    return ".." not in value


@dataclass(frozen=True)
class SiloSecurityProfile:
    """One silo's declared, authoritative security snapshot, parsed from the
    production registry. The fields are exactly what a
    :class:`~qdistro_guard_registry.FlowEndpoint` consults."""

    silo: str
    guards: frozenset[str] = frozenset()
    compartments: frozenset[str] = frozenset()
    conflict_classes: frozenset[str] = frozenset()

    def endpoint(self) -> FlowEndpoint:
        return FlowEndpoint(
            guards=self.guards,
            compartments=self.compartments,
            conflict_classes=self.conflict_classes,
        )


@dataclass(frozen=True)
class SiloSecuritySnapshot:
    """The OUTCOME of resolving a silo to a security snapshot.

    ``state`` is load-bearing: only ``resolved`` carries an authoritative
    endpoint; ``unresolved`` carries an EMPTY endpoint that is a placeholder for
    "unknown", never "clean". A caller that wants to feed the endpoint into
    ``record_chokepoint`` must go through :meth:`require_resolved`."""

    silo: str
    state: str
    endpoint: FlowEndpoint = field(default_factory=FlowEndpoint)
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.state == STATE_RESOLVED

    def require_resolved(self) -> FlowEndpoint:
        """Return the authoritative endpoint IFF resolved, else raise
        :class:`UnresolvedSilo`. The single sanctioned door from a snapshot to a
        FlowEndpoint a chokepoint may union — an unresolved snapshot can never
        slip an empty (clean-looking) endpoint through it."""
        if not self.resolved:
            raise UnresolvedSilo(
                f"silo {self.silo!r} security snapshot is {self.state!r}: "
                f"{self.reason or 'no authoritative security profile'} — refusing "
                f"to use an unresolved endpoint as authoritative (would launder "
                f"unknown classification to clean)"
            )
        return self.endpoint


def _unresolved(silo: str, reason: str) -> SiloSecuritySnapshot:
    return SiloSecuritySnapshot(
        silo=silo, state=STATE_UNRESOLVED, endpoint=FlowEndpoint(), reason=reason
    )


# --------------------------------------------------------------------------
# Registry loading (strict, fail-closed, ownership-checked)
# --------------------------------------------------------------------------


def _parse_str_set(name: str, table: dict, key: str, *,
                   validator, what: str) -> frozenset[str]:
    """Parse a list-of-slug field. Missing → empty (a silo may legitimately
    declare no compartments). Present-but-not-a-list, or any non-slug member,
    rejects the whole file (fail-closed)."""
    raw = table.get(key)
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise RegistryError(
            f"silo {name!r}: {key!r} must be a list, got {raw!r}")
    out: set[str] = set()
    for item in raw:
        if not validator(item):
            raise RegistryError(
                f"silo {name!r}: invalid {what} {item!r} in {key!r}")
        out.add(item)
    return frozenset(out)


def _parse_silo(name: str, table: object) -> SiloSecurityProfile:
    if not isinstance(table, dict):
        raise RegistryError(f"silo {name!r}: expected a table, got {table!r}")
    unknown = set(table) - _ALLOWED_KEYS
    if unknown:
        # A typo'd key (compartment, conflictClasses) must NOT be silently
        # ignored — it could leave a real field empty and silently under-classify
        # the silo. Reject (fail-closed), mirroring the disposable-classes loader.
        raise RegistryError(
            f"silo {name!r}: unknown key(s) {sorted(unknown)} "
            f"(allowed: {sorted(_ALLOWED_KEYS)})")

    # guards: REQUIRED to be a subset of the compiled guard vocabulary. A
    # security-authority file is the schema boundary; an unknown guard string
    # would make the config LOOK guarded while enforcing nothing (the registry's
    # evaluate_flow has no behaviour for it), so it rejects load. A new guard must
    # first land in RESERVED_GUARDS + GUARD_REGISTRY before a config may use it.
    raw_guards = table.get("guards")
    guards: set[str] = set()
    if raw_guards is not None:
        if not isinstance(raw_guards, list):
            raise RegistryError(
                f"silo {name!r}: 'guards' must be a list, got {raw_guards!r}")
        for g in raw_guards:
            if not isinstance(g, str):
                raise RegistryError(
                    f"silo {name!r}: guard {g!r} must be a string")
            if g not in RESERVED_GUARDS:
                raise RegistryError(
                    f"silo {name!r}: unknown guard {g!r} "
                    f"(reserved: {sorted(RESERVED_GUARDS)}); a guard must be in "
                    f"RESERVED_GUARDS + the behavioural registry before a config "
                    f"may use it")
            guards.add(g)

    compartments = _parse_str_set(
        name, table, "compartments", validator=_valid_slug, what="compartment")
    conflict_classes = _parse_str_set(
        name, table, "conflict_classes", validator=_valid_slug,
        what="conflict class")

    return SiloSecurityProfile(
        silo=name,
        guards=frozenset(guards),
        compartments=compartments,
        conflict_classes=conflict_classes,
    )


def _check_fd_ownership(fd: int, p: Path) -> None:
    """Refuse a registry whose OPEN fd is not a root-owned,
    non-group/world-writable REGULAR file. This file mints resolved
    FlowEndpoints, so a writable (or non-root-owned) file is equivalent to letting
    the writer mint compartments and guards.

    The check is done on the fd we will actually read (``fstat``), NOT on the
    pathname — together with ``O_NOFOLLOW`` at open this closes the lstat→open
    TOCTOU window: there is no second name lookup between the check and the read,
    so an attacker who can swap the dirent (e.g. when ``QDISTRO_SILO_SECURITY``
    points into a non-root-owned directory) cannot make us validate one inode and
    parse another.

    The uid-0-owner check is enforced only when the broker runs as root
    (``geteuid()==0``): an unprivileged dev/test run against a temp registry would
    otherwise always fail it. The group/world-writable check fires regardless of
    euid (a writable authority file is unsafe for any reader). This mirrors the
    conditional-on-root ownership posture used in ``qdistro_lineage_receipts``.
    """
    try:
        st = os.fstat(fd)
    except OSError as e:
        raise RegistryError(
            f"silo-security registry {p} unstattable: {e}") from e
    if not stat.S_ISREG(st.st_mode):
        # O_NOFOLLOW already refused a symlink at the final component; this
        # additionally refuses fifos/devices/dirs opened via O_NOFOLLOW.
        raise RegistryError(
            f"silo-security registry {p} is not a regular file")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RegistryError(
            f"silo-security registry {p} is group/world-writable "
            f"(mode {stat.S_IMODE(st.st_mode):04o}); refusing — a writable "
            f"authority file lets the writer mint guards/compartments")
    if os.geteuid() == 0 and st.st_uid != 0:
        raise RegistryError(
            f"silo-security registry {p} is owned by uid {st.st_uid}, not root; "
            f"refusing — a non-root-owned authority file is forgeable")


def load_registry(path: str | Path) -> dict[str, SiloSecurityProfile]:
    """Parse the silo-security TOML at ``path`` into
    ``{silo_name: SiloSecurityProfile}``.

    Fail-closed: any ownership/mode violation, read error, or malformed table
    raises :class:`RegistryError`, so a caller that cannot load the registry
    resolves every silo to ``unresolved`` rather than to a permissive clean
    default. An EMPTY ``[silo]`` table (no silos defined) is allowed — it simply
    resolves every silo to unresolved, which is fail-closed (vs the
    disposable-classes loader which requires >=1 class, because there an empty
    registry would brick every open; here an empty registry is the safe default).

    The file is opened with ``O_NOFOLLOW`` (refuse a symlink at the final
    component) and validated + parsed via the resulting fd, so the
    ownership/mode check and the read are on the SAME inode (no lstat→open
    TOCTOU).
    """
    p = Path(path)
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        # ELOOP here is O_NOFOLLOW refusing a symlink at the final component;
        # ENOENT is a missing file. Both fail closed.
        raise RegistryError(
            f"silo-security registry {p} unopenable (missing, a symlink, or "
            f"unreadable): {e}") from e
    try:
        _check_fd_ownership(fd, p)
        try:
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            # tomllib.load reads bytes and UTF-8-decodes internally, so a
            # non-UTF-8 (corrupt/binary) backing file raises UnicodeDecodeError,
            # which is NOT a TOMLDecodeError/OSError. Catch it too, or it would
            # escape RegistryError and propagate OUT of the SnapshotAuthority into
            # the chokepoint caller — a fail-OPEN of the fail-closed seam contract.
            raise RegistryError(
                f"silo-security registry {p} unreadable/malformed: {e}") from e
    finally:
        os.close(fd)

    silo_tbl = raw.get("silo")
    if silo_tbl is None:
        # No [silo.<name>] tables: a valid, fully-unresolved registry.
        return {}
    if not isinstance(silo_tbl, dict):
        raise RegistryError(f"registry {p}: [silo] is not a table")

    out: dict[str, SiloSecurityProfile] = {}
    for name, table in silo_tbl.items():
        if not _valid_slug(name):
            raise RegistryError(
                f"invalid silo name {name!r}: want lowercase "
                f"[a-z0-9][a-z0-9.-]* with no '..'")
        out[name] = _parse_silo(name, table)
    return out


def _registry_path(path: str | Path | None) -> str | Path:
    if path is not None:
        return path
    return os.environ.get(REGISTRY_PATH_ENV, REGISTRY_PATH_DEFAULT)


# --------------------------------------------------------------------------
# SnapshotAuthority seam — the resolver's production dependency
# --------------------------------------------------------------------------


class SnapshotAuthority(ABC):
    """The seam between the lineage resolver and the central silo-snapshot store.

    The resolver depends ONLY on this narrow interface — never on a concrete
    backing store, a filesystem path, an env var, or a raw registry dict. This is
    the forward-compat hedge for the decided control-plane direction
    (``todo/decisions/silo-snapshot-authority-control-plane.md``): a future
    control-plane daemon that owns/populates the central snapshot repository can
    be dropped in as a new ``SnapshotAuthority`` WITHOUT touching the resolver's
    fail-closed semantics.

    The behavioural contract a conforming authority MUST preserve:

    * :meth:`snapshot_for_silo` is given an ALREADY-VERIFIED, launcher-attested
      silo name (the caller — :func:`resolve_subject_silo_security` — has already
      proven the live pid → verified Subject → attested ``Subject.silo`` chain).
      The authority does NOT do subject verification; it never sees a
      caller-supplied silo string.
    * A silo with an authoritative profile → ``state="resolved"`` with that
      profile's endpoint (which may be empty: an authoritative "this silo has no
      guards", distinct from unknown).
    * An absent silo, a load/availability failure of the backing store, or any
      backend error → ``state="unresolved"`` with an EMPTY placeholder endpoint
      (unknown, NEVER clean). An authority MUST NOT raise into the resolver; it
      catches its own backend errors and returns an unresolved snapshot, so a
      broken store disables resolved lineage rather than crashing the chokepoint
      path.
    """

    @abstractmethod
    def snapshot_for_silo(self, silo: str) -> SiloSecuritySnapshot:
        """Resolve a verified, launcher-attested silo NAME to a snapshot.

        See the class contract: resolved iff the silo has an authoritative
        profile; unresolved (empty placeholder) for absent/unavailable/error.
        Never raises into the caller.
        """
        raise NotImplementedError


class InMemorySnapshotAuthority(SnapshotAuthority):
    """A :class:`SnapshotAuthority` over an in-memory ``{silo: profile}`` map.

    The test/seam reference implementation, and the shape a future daemon-fed
    in-process cache would take. Resolution is the pure
    :func:`resolve_silo_security`; this never touches the filesystem, so it cannot
    fail to load — it simply resolves absent silos to unresolved.
    """

    def __init__(self, registry: dict[str, SiloSecurityProfile] | None = None):
        self._registry = dict(registry or {})

    def snapshot_for_silo(self, silo: str) -> SiloSecuritySnapshot:
        return resolve_silo_security(silo, self._registry)


class TomlSnapshotAuthority(SnapshotAuthority):
    """The v1 BOOTSTRAP :class:`SnapshotAuthority`: the central snapshot store
    backed by the strict, root-owned, fail-closed TOML registry.

    This is the v1 implementation of the seam, NOT the design end state — a future
    control-plane daemon will own the central store behind the same interface (see
    the module docstring + the decision record). The TOML is reloaded per
    resolution by default (the file is small and admin-edited; a long-lived broker
    may pass a pre-loaded registry to :class:`InMemorySnapshotAuthority` instead);
    a load/ownership/parse failure fails CLOSED to ``unresolved`` and is logged
    loudly, never raised into the chokepoint path.
    """

    def __init__(self, path: str | Path | None = None):
        # Resolve the path lazily-at-construction (env override honoured here, at
        # the seam boundary — the resolver itself never consults the env).
        self._path = _registry_path(path)

    def snapshot_for_silo(self, silo: str) -> SiloSecuritySnapshot:
        # Pre-validate the verified silo at the seam so a load failure for an
        # already-unknown/empty silo is reported as the silo problem, not a
        # registry problem.
        if not silo or silo == UNKNOWN_SILO:
            return _unresolved(silo or UNKNOWN_SILO, "silo is unknown/unverified")
        try:
            registry = load_registry(self._path)
        except RegistryError as e:
            # A broken/unsafe authority store disables ALL resolved lineage —
            # fail closed to unresolved (never raise into the chokepoint path),
            # but make the misconfiguration LOUD so it is not silently swallowed.
            _log.warning(
                "silo-security snapshot store unavailable; resolving silo %r as "
                "unresolved (guard inheritance disabled until fixed): %s",
                silo, e)
            return _unresolved(silo, f"snapshot store unavailable: {e}")
        return resolve_silo_security(silo, registry)


def default_authority(path: str | Path | None = None) -> SnapshotAuthority:
    """The production v1 authority: :class:`TomlSnapshotAuthority` over the
    bootstrap store (env-overridable path). A future daemon-backed authority would
    be selected here without the resolver changing."""
    return TomlSnapshotAuthority(path)


# --------------------------------------------------------------------------
# Resolution (pure: registry dict in, snapshot out)
# --------------------------------------------------------------------------


def resolve_silo_security(
    silo: str,
    registry: dict[str, SiloSecurityProfile],
) -> SiloSecuritySnapshot:
    """Resolve a (presumed already verified) silo NAME against a loaded registry.

    PURE: dict in, snapshot out — unit-tests by feeding a dict. Fail-closed:

    * an empty silo (the :data:`~qdistro_resolver.UNKNOWN_SILO` an unverified
      subject carries) → ``unresolved`` (never matches a real silo);
    * a silo not present in the registry → ``unresolved`` (no authoritative
      profile);
    * a present silo → ``resolved`` with its declared endpoint.

    NOTE: this function does NOT itself verify the silo's provenance — it trusts
    that ``silo`` is the launcher-attested ``Subject.silo`` from a *verified*
    subject. The provenance verification is done by
    :func:`resolve_subject_silo_security`, which is the only safe entry point for
    a live caller. Calling this with a caller-supplied (untrusted) silo string
    would be a cross-silo source-forgery hole.
    """
    if not silo or silo == UNKNOWN_SILO:
        return _unresolved(silo or UNKNOWN_SILO, "silo is unknown/unverified")
    profile = registry.get(silo)
    if profile is None:
        return _unresolved(
            silo, "silo has no entry in the silo-security registry")
    return SiloSecuritySnapshot(
        silo=silo,
        state=STATE_RESOLVED,
        endpoint=profile.endpoint(),
        reason="resolved from silo-security registry",
    )


def resolve_subject_silo_security(
    pid: int,
    launch_store,
    *,
    authority: SnapshotAuthority | None = None,
    registry: dict[str, SiloSecurityProfile] | None = None,
    registry_path: str | Path | None = None,
) -> SiloSecuritySnapshot:
    """Resolve a LIVE ``pid`` to an authoritative silo security snapshot — the
    full three-link authority chain a live caller must use, and the ONLY safe
    entry point for a live caller.

    1. :func:`qdistro_resolver.resolve_subject` resolves the pid against
       ``launch_store`` to a :class:`~qdistro_resolver.Subject`; unless it is
       ``verified=True`` (matching, unexpired launch record + live kernel facts
       agree), its silo is :data:`~qdistro_resolver.UNKNOWN_SILO` (``""``).
    2. The launcher-attested ``Subject.silo`` (NEVER a caller-supplied string) is
       handed to the :class:`SnapshotAuthority` seam — the resolver does the
       VERIFICATION here, the authority does the lookup, and the authority never
       sees a caller-supplied silo string.
    3. The authority resolves a present silo; absent/unavailable → ``unresolved``.

    The ``authority`` is the production dependency (the central snapshot store
    behind the seam). For convenience/back-compat, a pre-loaded ``registry`` dict
    or a ``registry_path`` may be passed instead — they construct an
    :class:`InMemorySnapshotAuthority` / :class:`TomlSnapshotAuthority`
    respectively; if none is given, the default v1 authority
    (:func:`default_authority`, env-overridable TOML bootstrap store) is used. A
    broken/unsafe backing store fails closed to ``unresolved`` inside the
    authority — it never raises into this caller.
    """
    subject = resolve_subject(pid, launch_store)
    if not subject.verified:
        return _unresolved(
            UNKNOWN_SILO, f"subject not verified: {subject.reason}")
    silo = subject.silo
    if not silo or silo == UNKNOWN_SILO:
        # A verified subject with an empty silo (launcher registered no silo):
        # nothing authoritative to resolve.
        return _unresolved(
            UNKNOWN_SILO, "verified subject carries no silo")

    if authority is None:
        if registry is not None:
            authority = InMemorySnapshotAuthority(registry)
        else:
            authority = default_authority(registry_path)
    return authority.snapshot_for_silo(silo)
