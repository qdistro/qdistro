"""qdistro state-snapshot orchestration (fableplan2 task 05).

The template-triggered **pre-activation snapshot** for a binding generation
change, and the rollback **restore** that pairs an old generation with its
matching state. silos.md owns browser-profile snapshot POLICY (health checks,
lastReadySnapshot, profile-restore); this module owns ONLY the pre-activation
snapshot of a silo's state when its bound generation first activates, plus the
crash-consistent restore via :mod:`qdistro_snap_swap`.

Anchor + ordering (qdistro-resolve-binding --launch-env --record, which runs
in spawn-tier2 BEFORE podman run): take the snapshot FIRST; only after it
succeeds (or the policy waives it) does the caller commit the activation
marker and emit template.binding.activated. A failed strict snapshot leaves
the marker uncommitted so the next launch retries.

Snapshot storage (design note, fableplan2 task 05): snapshots are stored as a
read-only btrfs snapshot (or ``cp -a --reflink=auto`` copy on a non-btrfs
host) into a SIBLING ``<silo>/state-snapshots/<id>/snapshot`` directory — NOT
inside the live state subvolume via snapper's ``.snapshots`` child subvolume.
A ``.snapshots`` child cannot survive the RENAME_EXCHANGE atomic swap that
restore performs (the writable clone would lack it; the displaced state would
orphan it). doc/05 permits this sibling-dir fallback. snapper remains the
admin-app's full-rollback listing layer; wiring the broker's create-config /
delete surface to ALSO index these is VM-validation follow-up.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

import qdistro_templates as qt
import qdistro_template_audit as audit
# NB: qdistro_snap_swap lives under snapshots/, not templates/, so it is
# imported LAZILY inside restore_snapshot() — the launch anchor and promote
# import this module with only templates/ on the path (the probe's host mode),
# and only an actual --restore-state rollback needs snap-swap.

# Default rollback/retention window for state snapshots, in days. Mirrors the
# generation rollback window (promote.ROLLBACK_WINDOW_DAYS); kept here so the
# snapshot pin and the snapshot retention agree without importing promote.
SNAPSHOT_WINDOW_DAYS = int(os.environ.get("QDISTRO_ROLLBACK_WINDOW_DAYS", "14"))

SNAPSHOT_DIRNAME = "state-snapshots"
_WAIVER_PREFIX = ".waive-activation-"
_PIN_REASON = "pre-migration-snapshot"


class StateSnapshotError(Exception):
    """A pre-activation snapshot could not be taken."""


class StrictSnapshotRefused(StateSnapshotError):
    """Strict policy + snapshot failure: the new generation must NOT activate.
    The caller turns this into a launch refusal with the marker uncommitted."""


# --------------------------------------------------------------------------
# on-disk layout
# --------------------------------------------------------------------------

def snapshots_dir(layout: qt.Layout, silo: str) -> str:
    return os.path.join(layout.silo_dir(silo), SNAPSHOT_DIRNAME)


def _snapshot_path(layout: qt.Layout, silo: str, snap_id: str) -> str:
    qt.require_safe_name(snap_id, "snapshot id")
    return os.path.join(snapshots_dir(layout, silo), snap_id)


def _audit_db(layout: qt.Layout) -> str:
    return os.path.join(layout.var, "audit", "template_audit.sqlite")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _short(gen: str | None) -> str:
    if not gen:
        return "none"
    return gen.replace("sha256:", "")[:12]


# --------------------------------------------------------------------------
# snapshot materialization (the only mechanism-specific code)
# --------------------------------------------------------------------------

def _materialize(state_path: str, dest: str, mechanism: str) -> None:
    """Create a read-only point-in-time snapshot of ``state_path`` at
    ``dest``. subvolume → RO btrfs snapshot; directory → cp -a reflink copy
    (the honest ``mechanism = "copy"`` marker is recorded by the caller)."""
    if mechanism == "subvolume":
        btrfs = qt.resolve_btrfs()
        if not btrfs:
            raise StateSnapshotError(
                "state mechanism is subvolume but the btrfs CLI is absent")
        proc = subprocess.run(
            [btrfs, "subvolume", "snapshot", "-r", state_path, dest],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise StateSnapshotError(
                f"btrfs subvolume snapshot -r failed: {proc.stderr.strip()}")
    else:
        # cp -a preserves owner/mode/timestamps/ACLs/xattrs; --reflink=auto is
        # CoW where supported, a full copy otherwise.
        proc = subprocess.run(
            ["cp", "-a", "--reflink=auto", state_path, dest],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise StateSnapshotError(f"cp -a snapshot failed: {proc.stderr.strip()}")


def _snapshot_mechanism(state_path: str) -> str:
    """``copy`` for a plain directory, ``subvolume`` for a btrfs subvolume —
    read from the state-mechanism metadata task 01 wrote next to the state."""
    meta = qt.read_state_meta(state_path)
    if meta and meta.get("mechanism") == "subvolume":
        return "subvolume"
    return "copy"


# --------------------------------------------------------------------------
# snapshot records (list / lookup)
# --------------------------------------------------------------------------

def list_snapshots(layout: qt.Layout, silo: str) -> list[dict]:
    """All recorded state snapshots for a silo, newest first. Each entry is
    the meta dict plus ``id`` and ``path`` (the RO snapshot payload)."""
    root = snapshots_dir(layout, silo)
    out: list[dict] = []
    if not os.path.isdir(root):
        return out
    for snap_id in os.listdir(root):
        sdir = os.path.join(root, snap_id)
        meta_path = os.path.join(sdir, "meta.toml")
        if not os.path.isfile(meta_path):
            continue
        meta = qt.read_toml(meta_path)
        meta["id"] = snap_id
        meta["path"] = os.path.join(sdir, "snapshot")
        out.append(meta)
    out.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return out


def find_restore_snapshot(layout: qt.Layout, silo: str,
                          target_generation: str) -> dict | None:
    """The newest restore-eligible snapshot capturing the state as the
    ``target_generation`` left it (its ``outgoing_generation``). This is what
    ``promote --rollback <target> --restore-state`` restores."""
    for meta in list_snapshots(layout, silo):
        if (meta.get("outgoing_generation") == target_generation
                and meta.get("restore_eligible") == "true"
                and os.path.isdir(meta["path"])):
            return meta
    return None


# --------------------------------------------------------------------------
# break-glass waiver (admin-only, one-shot, audited)
# --------------------------------------------------------------------------

def _waiver_path(layout: qt.Layout, silo: str, incoming: str) -> str:
    return os.path.join(snapshots_dir(layout, silo),
                        f"{_WAIVER_PREFIX}{_short(incoming)}.toml")


def write_waiver(layout: qt.Layout, silo: str, incoming: str,
                 reason: str, *, now: float | None = None) -> str:
    """Record a one-shot waiver so the next launch activates ``incoming``
    WITHOUT a pre-activation snapshot. Break-glass — the reason is mandatory
    and the flip is recorded rollback-unavailable when consumed."""
    if not reason or not reason.strip():
        raise StateSnapshotError("--waive-activation-snapshot requires --reason")
    now = time.time() if now is None else now
    os.makedirs(snapshots_dir(layout, silo), mode=0o700, exist_ok=True)
    path = _waiver_path(layout, silo, incoming)
    qt.write_toml_atomic(path, {
        "silo": silo, "incoming_generation": incoming,
        "reason": reason.strip(), "created_at": _iso(now),
    }, 0o600)
    return path


def _consume_waiver(layout: qt.Layout, silo: str, incoming: str) -> dict | None:
    path = _waiver_path(layout, silo, incoming)
    if not os.path.isfile(path):
        return None
    waiver = qt.read_toml(path)
    if waiver.get("incoming_generation") != incoming:
        return None
    os.unlink(path)               # one-shot
    return waiver


# --------------------------------------------------------------------------
# the pre-activation snapshot
# --------------------------------------------------------------------------

def take_pre_activation_snapshot(layout: qt.Layout, silo: str, *,
                                 incoming_generation: str,
                                 outgoing_generation: str | None,
                                 template: str, state_path: str,
                                 policy: str,
                                 now: float | None = None) -> dict:
    """Snapshot the silo's current (outgoing) state before ``incoming`` first
    writes. Returns a result dict describing what happened; raises
    :class:`StrictSnapshotRefused` only when policy is strict AND the snapshot
    cannot be taken (the caller then refuses the launch).

    No snapshot is taken — and none is needed — on the genuinely first
    activation (no outgoing generation: there is no prior state to protect and
    nothing to roll back to)."""
    now = time.time() if now is None else now
    if outgoing_generation is None or outgoing_generation == incoming_generation:
        return {"taken": False, "reason": "first-activation-no-outgoing",
                "incoming": incoming_generation, "outgoing": outgoing_generation}

    # Break-glass: an admin pre-recorded a one-shot waiver for this flip. The
    # activation audit (the single template.binding.activated carrying
    # state_rollback=unavailable) is emitted ONCE by the launch anchor from
    # this result — not here, so the event stream shows exactly one activation.
    waiver = _consume_waiver(layout, silo, incoming_generation)
    if waiver is not None:
        return {"taken": False, "waived": True, "reason": waiver.get("reason"),
                "incoming": incoming_generation, "outgoing": outgoing_generation}

    if not os.path.isdir(state_path):
        # A missing state_path is task 01's hard launch error; treat a snapshot
        # of a non-existent state as a snapshot failure under the policy.
        return _on_failure(layout, silo, template, incoming_generation,
                           outgoing_generation, policy,
                           f"state_path {state_path} is not a directory")

    mechanism = _snapshot_mechanism(state_path)
    snap_id = f"{int(now)}-{_short(outgoing_generation)}"
    sdir = _snapshot_path(layout, silo, snap_id)
    dest = os.path.join(sdir, "snapshot")
    try:
        os.makedirs(sdir, mode=0o700, exist_ok=True)
        if os.path.exists(dest):
            # A retry after a prior crash mid-snapshot: drop the partial.
            _rm_snapshot_payload(dest)
        _materialize(state_path, dest, mechanism)
    except (StateSnapshotError, OSError) as exc:
        # Clean up a partial snapshot dir so a retry starts fresh.
        shutil.rmtree(sdir, ignore_errors=True)
        return _on_failure(layout, silo, template, incoming_generation,
                           outgoing_generation, policy, str(exc))

    # Snapshot succeeded: record its metadata, pin the outgoing generation, and
    # audit. The pin keeps the outgoing generation's IMAGE alive as long as the
    # snapshot — the rollback target and its matching state live and die
    # together.
    meta = {
        "silo": silo, "template": template,
        "outgoing_generation": outgoing_generation,
        "incoming_generation": incoming_generation,
        "mechanism": mechanism, "created_at": _iso(now),
        "state_path": state_path, "restore_eligible": "true",
        "expires_at": _iso(now + SNAPSHOT_WINDOW_DAYS * 86400),
    }
    qt.write_toml_atomic(os.path.join(sdir, "meta.toml"), meta, 0o600)
    _write_snapshot_pin(layout, template, outgoing_generation, silo, snap_id, now)
    audit.emit("template.state_snapshot.created", db_path=_audit_db(layout),
               silo=silo, template=template, generation=outgoing_generation,
               old_generation=outgoing_generation,
               new_generation=incoming_generation, result="created",
               reason=f"mechanism={mechanism}", evidence_path=sdir,
               snapshot_id=snap_id, broker=False)
    return {"taken": True, "id": snap_id, "path": dest, "mechanism": mechanism,
            "incoming": incoming_generation, "outgoing": outgoing_generation}


def _on_failure(layout, silo, template, incoming, outgoing, policy,
                detail) -> dict:
    if policy == "strict":
        raise StrictSnapshotRefused(
            f"pre-activation snapshot failed for silo {silo} "
            f"({outgoing} -> {incoming}): {detail}")
    # availability: proceed, but the flip is rollback-unavailable. The single
    # template.binding.activated (carrying state_rollback=unavailable) is
    # emitted by the launch anchor from this result, not here.
    return {"taken": False, "unavailable": True, "reason": detail,
            "incoming": incoming, "outgoing": outgoing}


def _write_snapshot_pin(layout, template, outgoing, silo, snap_id, now) -> None:
    pin = {
        "owner_type": "silo", "owner_id": silo, "reason": _PIN_REASON,
        "generation": outgoing, "template": template, "created_at": _iso(now),
        "expires_at": _iso(now + SNAPSHOT_WINDOW_DAYS * 86400),
        "snapshot_id": snap_id,
    }
    path = os.path.join(layout.pins_for(template, outgoing),
                        f"{_PIN_REASON}.toml")
    qt.write_pin(path, pin)


def _rm_snapshot_payload(path: str) -> None:
    """Remove a snapshot payload (RO subvolume or copied dir)."""
    btrfs = qt.resolve_btrfs()
    if btrfs:
        rc = subprocess.run([btrfs, "subvolume", "delete", path],
                            capture_output=True, text=True)
        if rc.returncode == 0:
            return
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# restore (promote --rollback --restore-state)
# --------------------------------------------------------------------------

def restore_snapshot(layout: qt.Layout, silo: str, snap_meta: dict,
                     state_path: str, *, now: float | None = None) -> dict:
    """Swap the chosen snapshot back into ``state_path`` crash-consistently
    via qdistro-snap-swap. ``state_path`` is never left missing; the displaced
    state is kept aside as ``state-rejected-<ts>``. Returns the snap-swap
    result augmented with the snapshot id."""
    now = time.time() if now is None else now
    src = snap_meta["path"]
    if not os.path.isdir(src):
        raise StateSnapshotError(
            f"snapshot payload {src} for silo {silo} is missing — cannot restore")
    try:                                    # lazy import: see module-top note
        import qdistro_snap_swap as snap_swap
    except ImportError:
        # Installed flat in libexec the plain import works; in the dev tree
        # snapshots/ is a sibling of templates/ — add it so --restore-state
        # works without the test sys.path wiring.
        import sys
        sib = os.path.join(os.path.dirname(os.path.dirname(__file__)), "snapshots")
        if sib not in sys.path:
            sys.path.insert(0, sib)
        import qdistro_snap_swap as snap_swap
    mechanism = snap_meta.get("mechanism", "copy")
    result = snap_swap.restore(src, state_path, mechanism=mechanism, now=now)
    audit.emit("template.state_snapshot.restored", db_path=_audit_db(layout),
               silo=silo, template=snap_meta.get("template"),
               generation=snap_meta.get("outgoing_generation"),
               old_generation=snap_meta.get("incoming_generation"),
               new_generation=snap_meta.get("outgoing_generation"),
               result="restored",
               reason=f"method={result['method']}; displaced kept at "
                      f"{result['rejected']}",
               evidence_path=os.path.dirname(src),
               snapshot_id=snap_meta.get("id"), broker=False)
    result["snapshot_id"] = snap_meta.get("id")
    return result
