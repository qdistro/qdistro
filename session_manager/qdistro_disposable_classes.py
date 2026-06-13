"""Disposable-class registry (07-disposables-plan P2 / "open-in-disposable").

The registry maps an *open class* (a mime-class or url-class such as
``agent-scratch``, ``text/plain``, ``url-preview-known-origin``, ``pdf``) to the
disposable it should open in: which tier-2 image ``workload`` to launch, the
network mode, and — load-bearing — the ``min_tier`` gate.

``min_tier`` is the HARD security property of this phase. Hostile-input classes
(``pdf``, ``office``, ``archive``) are present in the registry but DISABLED:
they carry ``min_tier`` above the maximum tier that exists today
(:data:`MAX_AVAILABLE_TIER` == 2). A tier-2 podman container shares the host
kernel, so it contains *accidents and blast radius*, not a hostile-document
parser adversary; shipping those classes at tier 2 would sell the Qubes-like
promise without the containment. They stay off until P3 lands VM-tier
disposables (which raises :data:`MAX_AVAILABLE_TIER`).

The gate is DATA, not prose: :func:`resolve_class` raises :class:`ClassDisabled`
whenever ``min_tier > max_tier``. The module is pure (no podman, no broker, no
clock) so the parse + the gate are unit-testable in isolation, and it
fail-closes on any malformed input — an unparseable file raises, a class missing
``min_tier`` is treated as DISABLED (never silently enabled by a typo).
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Import the workload validator so a registry workload obeys the exact same
# constraint the spawn path enforces (a workload becomes part of a container
# name + a broker action). Kept a sibling import so this module loads from both
# the source tree and the installed /usr/libexec/qdistro/ flat layout.
try:
    from qdistro_disposables import DisposableError, validate_workload
except ImportError:  # pragma: no cover - exercised only on a broken install
    import importlib.util as _ilu

    _here = Path(__file__).resolve().parent
    _spec = _ilu.spec_from_file_location(
        "qdistro_disposables", _here / "qdistro_disposables.py")
    assert _spec is not None and _spec.loader is not None
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    DisposableError = _mod.DisposableError  # type: ignore[assignment,misc]
    validate_workload = _mod.validate_workload  # type: ignore[assignment]


# The highest isolation tier whose disposable images exist TODAY. P1/P2 ship
# only tier-2 (podman) disposables, so this is 2. When P3 lands VM-tier
# (tier-4/5 transient libvirt domains) it is raised, which is the SINGLE edit
# that flips the hostile classes on — and only once a workload image for them
# also exists. Do not raise this without the VM-tier path.
MAX_AVAILABLE_TIER = 2

# The action namespace the broker gates open-in-disposable on. Rules-only and
# fail-closed in the broker (same set as qdistro.dispose.spawn:) — no rule means
# refused, and a cache row / hook verdict can never mint an open.
OPEN_ACTION_PREFIX = "qdistro.dispose.open:"

# Installed registry path. spawn-tier2's trusted open-gate and the SDK helper
# both read this unless overridden (env QDISTRO_DISPOSABLE_CLASSES).
REGISTRY_PATH_DEFAULT = "/etc/qdistro/disposable-classes.toml"

# Tiers the registry understands. 2 = podman (today); 4/5 = VM (P3, future).
_VALID_TIERS = frozenset((2, 4, 5))
# Network modes a class may declare. ``none`` (default-deny, no egress) or
# ``egress`` (the silo-egress contract: one route, one resolver, default-deny —
# url:* classes only). A disposable NEVER inherits the requester's egress.
_VALID_NETWORK = frozenset(("none", "egress"))

# Keys a class table may carry. An unknown key is a typo (``min_teir``) that
# could silently default-enable a hostile class — reject it (fail-closed).
_ALLOWED_KEYS = frozenset(("workload", "tier", "min_tier", "network"))
# A min_tier we substitute when a class omits it: higher than any real tier, so
# the class is DISABLED. We never default a missing min_tier to an enabled
# value. (Belt and suspenders: the loader rejects a missing min_tier outright;
# this constant documents the fail-closed direction.)
_DISABLED_MIN_TIER = 99

# HARD INVARIANT (codex design review §1). The registry file is admin-editable
# local policy, so the min_tier gate alone would let an admin or a packaging
# mistake lower a hostile-input class to tier 2 (e.g. ``[classes.pdf] min_tier =
# 2``) — silently selling the Qubes-like promise without the containment. These
# classes carry a code-enforced FLOOR: the loader rejects any registry entry
# that sets their min_tier below the floor (or their tier below it). This keeps
# the gate data-driven for ordinary classes while making hostile tier-2
# enablement fail-closed on a typo or a bad local edit — it is impossible to
# open pdf/office/archive at tier 2 without editing THIS source AND shipping a
# VM-tier image. The floor is 4 (the lowest VM tier).
HOSTILE_CLASS_MIN_TIER: dict[str, int] = {
    "pdf": 4,
    "office": 4,
    "archive": 4,
}


class RegistryError(ValueError):
    """The registry file is malformed / unloadable (fail-closed: refuse all)."""


class UnknownClass(KeyError):
    """The requested class is not in the registry."""


class ClassDisabled(PermissionError):
    """The class exists but is gated off at the current max tier (min_tier
    above MAX_AVAILABLE_TIER). This is the hostile-class containment: pdf /
    office / archive resolve here until VM-tier disposables ship."""


@dataclass(frozen=True)
class DisposableClass:
    name: str
    workload: str
    tier: int
    min_tier: int
    network: str

    def is_enabled(self, max_tier: int = MAX_AVAILABLE_TIER) -> bool:
        return self.min_tier <= max_tier


def _coerce_int(value: object, *, field: str, cls: str) -> int:
    # tomllib already types integers; reject anything else (a quoted "2" or a
    # float) rather than coercing — a coercion could turn a malformed value into
    # a permissive tier.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryError(
            f"class {cls!r}: {field} must be an integer, got {value!r}")
    return value


def _parse_class(name: str, table: object) -> DisposableClass:
    if not isinstance(table, dict):
        raise RegistryError(f"class {name!r}: expected a table, got {table!r}")
    unknown = set(table) - _ALLOWED_KEYS
    if unknown:
        # A typo'd key (e.g. min_teir) must not be silently ignored — it could
        # leave the real min_tier at its default and enable a hostile class.
        raise RegistryError(
            f"class {name!r}: unknown key(s) {sorted(unknown)} "
            f"(allowed: {sorted(_ALLOWED_KEYS)})")

    # workload: required, and must pass the SAME validator the spawn path uses.
    workload = table.get("workload")
    if not isinstance(workload, str):
        raise RegistryError(f"class {name!r}: missing/invalid 'workload'")
    try:
        validate_workload(workload)
    except DisposableError as e:
        raise RegistryError(f"class {name!r}: invalid workload: {e}") from e

    # min_tier: REQUIRED. A missing min_tier is fail-closed — we refuse to load
    # rather than guess, because guessing low would enable a class. (If a future
    # caller wants lenient loading, _DISABLED_MIN_TIER is the only safe default.)
    if "min_tier" not in table:
        raise RegistryError(
            f"class {name!r}: 'min_tier' is required (the enablement gate); "
            f"a class with no min_tier is refused so a hostile class can never "
            f"be silently enabled")
    min_tier = _coerce_int(table["min_tier"], field="min_tier", cls=name)
    if min_tier not in _VALID_TIERS and min_tier < _DISABLED_MIN_TIER:
        raise RegistryError(
            f"class {name!r}: min_tier {min_tier} not one of "
            f"{sorted(_VALID_TIERS)}")

    # tier: required, the tier the class actually runs at when enabled.
    if "tier" not in table:
        raise RegistryError(f"class {name!r}: 'tier' is required")
    tier = _coerce_int(table["tier"], field="tier", cls=name)
    if tier not in _VALID_TIERS:
        raise RegistryError(
            f"class {name!r}: tier {tier} not one of {sorted(_VALID_TIERS)}")

    # Hostile-class FLOOR (codex review §1 / the load-bearing safety property).
    # A hostile-input class can never be configured below its code-enforced
    # floor — neither its min_tier (the gate) nor its actual run tier may drop
    # below it. So pdf/office/archive are un-enableable at tier 2 even if an
    # admin (or a packaging bug) edits the registry to say min_tier = 2.
    floor = HOSTILE_CLASS_MIN_TIER.get(name)
    if floor is not None:
        if min_tier < floor:
            raise RegistryError(
                f"class {name!r} is a hostile-input class: its min_tier "
                f"({min_tier}) may not be lowered below {floor} — it stays "
                f"VM-tier-only until P3 ships VM disposables (refusing to "
                f"enable a hostile parser in a shared-kernel tier-2 container)")
        if tier < floor:
            raise RegistryError(
                f"class {name!r} is a hostile-input class: its tier ({tier}) "
                f"may not be below {floor} (must run at the VM tier)")

    # network: optional, defaults to the secure 'none'.
    network = table.get("network", "none")
    if network not in _VALID_NETWORK:
        raise RegistryError(
            f"class {name!r}: network {network!r} not one of "
            f"{sorted(_VALID_NETWORK)}")

    return DisposableClass(
        name=name, workload=workload, tier=tier, min_tier=min_tier,
        network=network)


def load_classes(path: str | Path) -> dict[str, DisposableClass]:
    """Parse the registry TOML at ``path`` into ``{class_name: DisposableClass}``.

    Fail-closed: any read error or malformed table raises :class:`RegistryError`,
    so a caller that can't load the registry refuses every open rather than
    falling back to a permissive default.
    """
    p = Path(path)
    try:
        with open(p, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as e:
        raise RegistryError(f"registry not found: {p}") from e
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RegistryError(f"registry {p} unreadable/malformed: {e}") from e

    classes_tbl = raw.get("classes")
    if classes_tbl is None:
        raise RegistryError(f"registry {p}: missing top-level [classes] table")
    if not isinstance(classes_tbl, dict):
        raise RegistryError(f"registry {p}: [classes] is not a table")

    out: dict[str, DisposableClass] = {}
    for name, table in classes_tbl.items():
        # The class name becomes the suffix of a broker action
        # (qdistro.dispose.open:<name>). Constrain it: lowercase alnum / dot /
        # slash / hyphen (covers mime-classes like text/plain and url-classes
        # like url-preview-known-origin), no whitespace, no control bytes, no
        # ``..`` path traversal.
        if not _valid_class_name(name):
            raise RegistryError(
                f"invalid class name {name!r}: want lowercase "
                f"[a-z0-9][a-z0-9./-]* with no '..'")
        out[name] = _parse_class(name, table)
    if not out:
        raise RegistryError(f"registry {p}: no classes defined")
    return out


# Class name: a mime-class (text/plain) or url-class (url-preview-known-origin)
# or a bare slug (agent-scratch). Lowercase, starts alnum, then alnum/./-/slash;
# bounded length; no consecutive '..' (path-traversal hygiene since the name
# rides into an action string and could be logged/pathed downstream).
_CLASS_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9./-]{0,62}$")


def _valid_class_name(name: object) -> bool:
    if not isinstance(name, str) or not _CLASS_NAME_RE.fullmatch(name):
        return False
    if ".." in name:
        return False
    return True


def open_action(class_name: str) -> str:
    """The broker open-gate action for a class: ``qdistro.dispose.open:<class>``.

    Validates the class-name shape so a malformed name can never be smuggled
    into an action string (mirrors :func:`qdistro_disposables.dispose_action`)."""
    if not _valid_class_name(class_name):
        raise RegistryError(f"invalid class name {class_name!r}")
    return f"{OPEN_ACTION_PREFIX}{class_name}"


def resolve_class(name: str, classes: dict[str, DisposableClass], *,
                  max_tier: int = MAX_AVAILABLE_TIER) -> DisposableClass:
    """Return the :class:`DisposableClass` for ``name``, enforcing the
    enablement gate.

    Raises :class:`UnknownClass` if ``name`` is not in the registry, or
    :class:`ClassDisabled` if its ``min_tier`` is above ``max_tier`` (the
    hostile-class containment: pdf / office / archive are disabled at the
    tier-2 default). The gate is data-driven — flipping a class on requires
    raising its ``min_tier`` ceiling in the registry AND raising ``max_tier``
    (i.e. shipping the VM-tier path), never a code edit here.
    """
    cls = classes.get(name)
    if cls is None:
        raise UnknownClass(name)
    if not cls.is_enabled(max_tier):
        raise ClassDisabled(
            f"class {name!r} requires tier >= {cls.min_tier} but the maximum "
            f"available disposable tier is {max_tier}; it stays disabled until "
            f"VM-tier disposables ship (07-disposables-plan P3)")
    return cls


def resolve_from_registry(name: str, *,
                          path: str | Path = REGISTRY_PATH_DEFAULT,
                          max_tier: int = MAX_AVAILABLE_TIER) -> DisposableClass:
    """Load the registry at ``path`` and resolve ``name`` with the enablement
    gate. Convenience wrapper that fail-closes on a malformed registry
    (:class:`RegistryError`), an unknown class (:class:`UnknownClass`), or a
    gated-off class (:class:`ClassDisabled`)."""
    return resolve_class(name, load_classes(path), max_tier=max_tier)


def _main(argv: list[str]) -> int:
    """Tiny CLI for the trusted bash launch path (spawn-tier2.sh).

    ``--resolve <class>`` prints ``WORKLOAD=<w>``/``NETWORK=<n>``/``TIER=<t>``/
    ``OPEN_ACTION=<a>`` KEY=VALUE lines and exits 0 IFF the class is in the
    registry AND enabled at the current max tier. Exit codes:
      0  enabled       -> KEY=VALUE plan on stdout
      3  unknown class
      4  class disabled (hostile-class gate / min_tier)
      5  malformed registry (fail-closed: refuse all)
      2  usage / other error
    Diagnostics go to stderr; the trusted caller keys on the exit code, never
    the prose. This stays fail-closed: any non-zero exit means "do not open".
    """
    import argparse

    ap = argparse.ArgumentParser(prog="qdistro-disposable-classes")
    ap.add_argument("--resolve", metavar="CLASS", required=True)
    ap.add_argument("--registry", default=os.environ.get(
        "QDISTRO_DISPOSABLE_CLASSES", REGISTRY_PATH_DEFAULT))
    ap.add_argument("--max-tier", type=int, default=MAX_AVAILABLE_TIER)
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    try:
        cls = resolve_from_registry(
            args.resolve, path=args.registry, max_tier=args.max_tier)
    except RegistryError as e:
        print(f"qdistro-disposable-classes: malformed registry: {e}",
              file=sys.stderr)
        return 5
    except UnknownClass:
        print(f"qdistro-disposable-classes: unknown class {args.resolve!r}",
              file=sys.stderr)
        return 3
    except ClassDisabled as e:
        print(f"qdistro-disposable-classes: class disabled: {e}",
              file=sys.stderr)
        return 4

    print(f"CLASS={cls.name}")
    print(f"WORKLOAD={cls.workload}")
    print(f"NETWORK={cls.network}")
    print(f"TIER={cls.tier}")
    print(f"OPEN_ACTION={open_action(cls.name)}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(_main(sys.argv[1:]))
