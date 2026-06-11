"""qdistro-template-gc — retention enforcement for template payloads
(todo/fableplan task 07).

Deletion here is security-critical: collecting a generation deletes
someone's rollback target. So the order is strict:

1. Build the pinned set from bindings and pin receipts FIRST. A binding's
   active generation is always pinned; any generation with an unexpired
   receipt is untouchable regardless of retention counts. An `active` pin
   only protects a generation that still matches a binding (stale active
   pins from a mid-promote crash do not).
2. Then apply /etc/qdistro/template-retention.toml: keep N promoted
   generations, collect failed candidate payloads after their window.
3. Delete PAYLOADS ONLY — the podman image. Never evidence, never
   manifests, never audit records. Each deletion writes a
   `template.gc.deleted` event referencing the evidence that remains.

Fail closed: any parse error in a pin receipt, binding, or the retention
file aborts the whole run before deleting anything — a corrupt pin must
never read as "unpinned".
"""
from __future__ import annotations

import argparse
import calendar
import os
import subprocess
import sys
import time

import qdistro_templates as qt
import qdistro_template_audit as audit
import qdistro_state_snapshot as state_snapshot

# A snapshot dir that has a payload but no meta.toml is a partial left by a
# crash between materializing the payload and writing its metadata. Reap it
# once it is older than this grace window so an IN-PROGRESS snapshot (payload
# written, meta about to be written) is never reaped mid-creation.
_PARTIAL_SNAPSHOT_GRACE_SECONDS = 3600

DEFAULT_RETENTION = {
    "keep_promoted_generations": 3,
    "keep_promoted_generations_vm": 2,
    "failed_candidate_days": 7,
    "build_log_days": 180,
    "audit_evidence_years": 3,
}


def log(msg: str) -> None:
    print(f"[template-gc] {msg}", file=sys.stderr, flush=True)


def _audit_db(layout: qt.Layout) -> str:
    return os.path.join(layout.var, "audit", "template_audit.sqlite")


def _parse_expiry(value: object) -> float:
    """Parse an ISO8601 'Z' timestamp to epoch seconds. Raises on a
    malformed or non-string value (fail closed — a corrupt expiry must not
    read as expired or unexpired by accident, nor crash with TypeError)."""
    if not isinstance(value, str):
        raise qt.TemplateError(f"pin expires_at must be a string, got {value!r}")
    return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))


def load_retention(layout: qt.Layout) -> dict:
    if not os.path.isfile(layout.retention_file):
        return dict(DEFAULT_RETENTION)
    # A present-but-malformed retention file is a hard error (fail closed).
    return qt.validate_retention(qt.read_toml(layout.retention_file))


def _binding_actives(layout: qt.Layout) -> set[tuple[str, str]]:
    actives = set()
    if not os.path.isdir(layout.bindings_dir):
        return actives
    for name in os.listdir(layout.bindings_dir):
        # Skip atomic-write temp files a crash may have left behind; they are
        # by construction never committed state and must not wedge GC.
        if name.startswith(".tmp-") or not name.endswith(".toml"):
            continue
        binding = qt.read_binding(os.path.join(layout.bindings_dir, name))
        actives.add((binding["template"], binding["active_generation"]))
    return actives


def build_pinned_set(layout: qt.Layout, now: float) -> set[tuple[str, str]]:
    """(template, generation) pairs that must NOT be collected. Fail-closed
    on any unparseable binding or pin receipt."""
    actives = _binding_actives(layout)
    pinned = set(actives)  # the live launch target is always pinned
    if not os.path.isdir(layout.pins_dir):
        return pinned
    for template in os.listdir(layout.pins_dir):
        tdir = os.path.join(layout.pins_dir, template)
        if not os.path.isdir(tdir):
            continue
        for gen in os.listdir(tdir):
            gdir = os.path.join(tdir, gen)
            if not os.path.isdir(gdir):
                continue
            for pin_name in os.listdir(gdir):
                if pin_name.startswith(".tmp-") or not pin_name.endswith(".toml"):
                    continue
                pin = qt.validate_pin(qt.read_toml(os.path.join(gdir, pin_name)))
                # Reconcile the receipt's contents with its path: a valid but
                # misplaced receipt must not pin the wrong generation (and
                # thereby leave the generation it actually names unpinned).
                if pin["template"] != template or pin["generation"] != gen:
                    raise qt.TemplateError(
                        f"pin receipt at {os.path.join(gdir, pin_name)} names "
                        f"{pin['template']}/{pin['generation']} but is filed "
                        f"under {template}/{gen}")
                key = (template, gen)
                if pin["reason"] == "active":
                    # Reconcile against bindings: a stale active pin (left by
                    # a mid-promote crash) whose generation is not a binding's
                    # active does NOT protect anything.
                    if key in actives:
                        pinned.add(key)
                    continue
                expires = pin.get("expires_at")
                if expires is None or _parse_expiry(expires) > now:
                    pinned.add(key)
    return pinned


def _keep_count(retention: dict, template: str) -> int:
    # retention (incl. its overrides tree) is fully validated by
    # load_retention before any deletion, so these are already non-negative
    # ints — no late conversion that could raise mid-run.
    overrides = retention.get("overrides", {}).get(template, {})
    return overrides.get("keep_promoted_generations",
                         retention["keep_promoted_generations"])


def _image_exists(image_ref: str) -> bool:
    proc = subprocess.run(["podman", "image", "exists", image_ref],
                          capture_output=True, text=True)
    return proc.returncode == 0


def _rmi(image_ref: str) -> bool:
    proc = subprocess.run(["podman", "rmi", image_ref],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"WARN: podman rmi {image_ref} failed: {proc.stderr.strip()}")
        return False
    return True


# Protective tagging (cascade guard). Generation images are stored UNTAGGED
# (build drops the candidate tag so GC can `podman rmi <digest>`), and
# `podman rmi <child>` cascade-deletes an UNTAGGED, otherwise-unreferenced
# PARENT image. So collecting a failed candidate (or an unpinned generation)
# that was built FROM a pinned generation would drag the pinned generation's
# image down with it — silently losing a rollback target whose only protection
# is a pin (the pin guards the RECORD, not the image's layer parentage). A tag
# on the parent stops the cascade; GC tags every pinned generation image for
# the duration of its rmi sweep and removes the tag afterwards.
_PROTECT_REPO = "qdistro-gc-protect"


def _protect_tag(digest: str) -> str:
    # A unique, valid local tag derived from the digest hex (tags may not
    # contain ':' beyond the repo:tag separator, so strip the algo prefix).
    return f"{_PROTECT_REPO}:{digest.split(':')[-1]}"


def _tag(digest: str) -> str | None:
    tag = _protect_tag(digest)
    proc = subprocess.run(["podman", "tag", digest, tag],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"WARN: podman tag {digest} {tag} failed: {proc.stderr.strip()}")
        return None
    return tag


def _untag(tag: str) -> None:
    subprocess.run(["podman", "untag", tag], capture_output=True, text=True)


def _all_pinned_digests(pinned: set[tuple[str, str]]) -> set[str]:
    """Just the generation digests from the pinned (template, gen) set,
    flattened across templates. A candidate's payload (its config digest)
    can be shared with a live pinned generation even under a *different*
    template (a cache-identical rebuild yields the same image_id), so the
    candidate payload is protected if its digest matches ANY pinned gen."""
    return {gen for (_template, gen) in pinned}


def _all_promoted_generations(layout: qt.Layout) -> set[str]:
    """Every digest that has a materialized generation record under any
    template. The generation-retention path owns those payloads; a failed
    candidate sharing such a digest must not delete the live generation's
    image out from under it."""
    promoted: set[str] = set()
    if not os.path.isdir(layout.templates_var):
        return promoted
    for template in os.listdir(layout.templates_var):
        gens_dir = layout.generations_dir(template)
        if not os.path.isdir(gens_dir):
            continue
        for gen in os.listdir(gens_dir):
            if os.path.isdir(os.path.join(gens_dir, gen)):
                promoted.add(gen)
    return promoted


def _delete_generation_payload(layout: qt.Layout, template: str, gen: str,
                               reason: str, dry_run: bool, rmi) -> dict:
    gen_dir = layout.generation_dir(template, gen)
    evidence = os.path.join(gen_dir, "evidence")
    deletion = {"kind": "generation", "template": template, "generation": gen,
                "reason": reason, "evidence_path": evidence, "deleted": False}
    if dry_run:
        log(f"DRY-RUN would delete generation payload {gen} ({reason}); "
            f"evidence kept at {evidence}")
        return deletion
    deletion["deleted"] = rmi(gen)  # gen is the sha256 image ref
    if not deletion["deleted"]:
        # rmi failed (e.g. image still in use). The on-disk record is intact;
        # do not write a false "deleted" audit row — just log and move on.
        log(f"NOT deleted: generation payload {gen} (podman rmi failed)")
        return deletion
    # Evidence + manifest stay; only the image payload is removed.
    audit.emit("template.gc.deleted", db_path=_audit_db(layout),
               template=template, generation=gen, result="deleted",
               reason=reason, evidence_path=evidence)
    log(f"deleted generation payload {gen} ({reason}); evidence kept at {evidence}")
    return deletion


def _candidate_payload_digest(cdir: str) -> str | None:
    """The candidate's launchable payload digest (the manifest's
    generation_ref == image_id config digest), or None when there is no
    payload to collect.

    A failed *build* leaves no manifest and no image — that is "nothing to
    collect", not an error. A manifest we cannot trust (corrupt/unparseable,
    or filing a digest the schema rejects) must NOT read as collectable:
    deleting on the strength of a manifest you can't trust is worse than
    leaving the payload, so fail closed by raising back to the per-candidate
    skip in gc()."""
    man = os.path.join(cdir, "manifest.toml")
    if not os.path.isfile(man):
        return None
    return qt.generation_ref(qt.read_manifest(man))


def _delete_candidate_payload(layout: qt.Layout, template: str, run_id: str,
                              cdir: str, digest: str, dry_run: bool,
                              rmi, image_exists) -> dict:
    evidence = cdir  # the candidate dir IS the evidence (build.log, manifest, report)
    deletion = {"kind": "candidate", "template": template, "run_id": run_id,
                "generation": digest, "reason": "failed-candidate-expired",
                "evidence_path": evidence, "deleted": False}
    if dry_run:
        log(f"DRY-RUN would delete failed candidate payload {run_id} "
            f"(image {digest}); evidence kept at {evidence}")
        return deletion
    # An already-absent image (rmi'd by an earlier run, or never pushed past
    # a build that produced no image) is already-collected — silent, no WARN,
    # no audit row. Only an extant payload gets an rmi attempt.
    if not image_exists(digest):
        log(f"already collected: failed candidate payload {run_id} "
            f"(image {digest} absent); evidence kept at {evidence}")
        return deletion
    deletion["deleted"] = rmi(digest)
    if not deletion["deleted"]:
        log(f"NOT deleted: failed candidate payload {run_id} (podman rmi failed)")
        return deletion
    audit.emit("template.gc.deleted", db_path=_audit_db(layout),
               template=template, generation=digest, run_id=run_id,
               result="deleted", reason="failed-candidate-expired",
               evidence_path=evidence)
    log(f"deleted failed candidate payload {run_id} (image {digest}); "
        f"evidence kept at {evidence}")
    return deletion


def _collect_state_snapshots(layout: qt.Layout, now: float,
                             dry_run: bool) -> list[dict]:
    """Retention for fableplan2 task-05 pre-activation state snapshots.

    A snapshot is deleted once its own ``expires_at`` (baked at creation =
    created_at + rollback window) has passed. Deletion removes the snapshot
    PAYLOAD and drops it from rollback choices; ONLY the audit metadata
    (mechanism, source, generations, created/deleted times) is kept — this is
    USER STATE, so the record must not imply the data survives. (The matching
    pre-migration-snapshot pin shares the same expiry and lapses in step, so
    the outgoing generation's image becomes collectable too.)"""
    deletions: list[dict] = []
    silos_dir = layout.silos_dir
    if not os.path.isdir(silos_dir):
        return deletions
    for silo in sorted(os.listdir(silos_dir)):
        snaps_dir = os.path.join(silos_dir, silo,
                                 state_snapshot.SNAPSHOT_DIRNAME)
        if not os.path.isdir(snaps_dir):
            continue
        for snap_id in sorted(os.listdir(snaps_dir)):
            sdir = os.path.join(snaps_dir, snap_id)
            meta_path = os.path.join(sdir, "meta.toml")
            payload = os.path.join(sdir, "snapshot")
            if not os.path.isfile(meta_path):
                # No meta. A leaked partial (payload written, crash before
                # meta) holds user state invisible to list/find/retention —
                # reap it once past the grace window, audited. (A dir with no
                # payload either is being created or is already cleaned; leave
                # it. Waiver files .waive-*.toml are not directories, so the
                # isdir(payload) guard skips them.)
                if (os.path.isdir(payload)
                        and os.path.getmtime(sdir) < now - _PARTIAL_SNAPSHOT_GRACE_SECONDS):
                    deletion = {"kind": "state-snapshot", "silo": silo,
                                "template": None, "generation": None,
                                "snapshot_id": snap_id,
                                "reason": "partial-no-meta",
                                "evidence_path": sdir, "deleted": False}
                    if dry_run:
                        log(f"DRY-RUN would reap partial state snapshot "
                            f"{snap_id} for silo {silo} (payload, no meta)")
                    else:
                        _rm_snapshot_payload(payload)
                        deletion["deleted"] = True
                        audit.emit("template.gc.deleted", db_path=_audit_db(layout),
                                   silo=silo, result="deleted",
                                   reason="state-snapshot-partial",
                                   evidence_path=sdir, kind="state-snapshot",
                                   snapshot_id=snap_id)
                        log(f"reaped partial (meta-less) state snapshot "
                            f"{snap_id} for silo {silo}")
                    deletions.append(deletion)
                continue
            meta = qt.read_toml(meta_path)            # fail-closed on corrupt
            expires = meta.get("expires_at")
            if expires is None or _parse_expiry(expires) > now:
                continue  # unexpired — still a rollback target
            if not os.path.isdir(payload):
                continue  # already collected; audit metadata remains
            deletion = {"kind": "state-snapshot", "silo": silo,
                        "template": meta.get("template"),
                        "generation": meta.get("outgoing_generation"),
                        "snapshot_id": snap_id, "reason": "snapshot-expired",
                        "evidence_path": meta_path, "deleted": False}
            if dry_run:
                log(f"DRY-RUN would delete expired state snapshot {snap_id} for "
                    f"silo {silo} (user state; audit metadata kept)")
                deletions.append(deletion)
                continue
            _rm_snapshot_payload(payload)
            # Honesty: keep the metadata as the audit record, mark it
            # ineligible + when it was deleted; the user state itself is GONE.
            meta["restore_eligible"] = "false"
            meta["deleted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(now))
            qt.write_toml_atomic(meta_path, meta, 0o600)
            deletion["deleted"] = True
            audit.emit("template.gc.deleted", db_path=_audit_db(layout),
                       silo=silo, template=meta.get("template"),
                       generation=meta.get("outgoing_generation"),
                       result="deleted", reason="state-snapshot-expired",
                       evidence_path=meta_path, kind="state-snapshot",
                       snapshot_id=snap_id)
            log(f"deleted expired state snapshot {snap_id} for silo {silo} "
                f"(user state gone; audit metadata kept at {meta_path})")
            deletions.append(deletion)
    return deletions


def _rm_snapshot_payload(path: str) -> None:
    btrfs = subprocess.run(["sh", "-c", "command -v btrfs"],
                           capture_output=True, text=True).stdout.strip()
    if btrfs:
        rc = subprocess.run([btrfs, "subvolume", "delete", path],
                            capture_output=True, text=True)
        if rc.returncode == 0:
            return
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def gc(layout: qt.Layout | None = None, *, dry_run: bool = False,
       now: float | None = None, rmi=_rmi, image_exists=_image_exists,
       tag=_tag, untag=_untag) -> list[dict]:
    layout = layout or qt.Layout()
    now = time.time() if now is None else now
    retention = load_retention(layout)            # fail-closed
    pinned = build_pinned_set(layout, now)        # fail-closed
    pinned_digests = _all_pinned_digests(pinned)
    promoted_digests = _all_promoted_generations(layout)
    deletions: list[dict] = []
    # Cascade guard: protect every pinned generation image with a transient tag
    # so a later `rmi` of a child (a failed candidate built FROM it, or an
    # unpinned generation layered on it) cannot cascade-delete it. A pin guards
    # the generation RECORD; only a tag guards the IMAGE from podman's
    # untagged-parent cleanup. Skipped in dry-run (it deletes nothing).
    protect_tags: list[str] = []
    if not dry_run:
        for digest in pinned_digests:
            if image_exists(digest):
                t = tag(digest)
                if t is None:
                    # Fail closed: a pinned generation image we could NOT
                    # protect must never be left exposed to a cascading rmi
                    # (that is the exact data-loss this guard prevents). Untag
                    # what we already protected and abort before deleting
                    # anything — a transient `podman tag` failure must not
                    # silently degrade into "collect the pinned rollback
                    # target's image too".
                    for done in protect_tags:
                        untag(done)
                    raise qt.TemplateError(
                        f"could not tag-protect pinned generation image "
                        f"{digest} from GC cascade; aborting before any "
                        f"deletion")
                protect_tags.append(t)
    try:
        deletions.extend(_gc_payloads(layout, retention, pinned, pinned_digests,
                                      promoted_digests, now, dry_run, rmi,
                                      image_exists))
    finally:
        for t in protect_tags:
            untag(t)
    return deletions


def _gc_payloads(layout: qt.Layout, retention: dict, pinned, pinned_digests,
                 promoted_digests, now: float, dry_run: bool, rmi,
                 image_exists) -> list[dict]:
    deletions: list[dict] = []
    # State-snapshot retention (task 05) is independent of the template payload
    # passes: expired user-state snapshots are collected by their own window,
    # keeping only audit metadata. Run it first so a silo with no template
    # payloads (templates_var absent) still gets its snapshots reaped.
    deletions.extend(_collect_state_snapshots(layout, now, dry_run))
    if not os.path.isdir(layout.templates_var):
        return deletions

    for template in sorted(os.listdir(layout.templates_var)):
        gens_dir = layout.generations_dir(template)
        if os.path.isdir(gens_dir):
            keep_n = _keep_count(retention, template)
            gens = [(g, os.path.join(gens_dir, g)) for g in os.listdir(gens_dir)
                    if os.path.isdir(os.path.join(gens_dir, g))]
            # Newest first by materialization mtime, with the digest as a
            # deterministic tie-breaker so equal mtimes give a stable order.
            gens.sort(key=lambda gd: (os.path.getmtime(gd[1]), gd[0]), reverse=True)
            for i, (gen, _gen_dir) in enumerate(gens):
                if (template, gen) in pinned:
                    continue  # untouchable, regardless of retention count
                if i < keep_n:
                    continue  # within the keep-N window
                deletions.append(_delete_generation_payload(
                    layout, template, gen, "beyond-retention", dry_run, rmi))

        cands_dir = layout.candidates_dir(template)
        if os.path.isdir(cands_dir):
            cutoff = now - retention["failed_candidate_days"] * 86400
            for run_id in os.listdir(cands_dir):
                cdir = os.path.join(cands_dir, run_id)
                if not os.path.isdir(cdir):
                    continue
                if qt.candidate_state(cdir) != "failed":
                    continue
                if os.path.getmtime(cdir) >= cutoff:
                    continue
                try:
                    digest = _candidate_payload_digest(cdir)
                except (qt.TemplateError, ValueError) as exc:
                    # A corrupt manifest (TemplateError) or unparseable TOML
                    # (tomllib raises a ValueError subclass) must not abort the
                    # run nor read as collectable — skip this candidate, fail
                    # closed. (A pin/binding/retention parse error still aborts;
                    # this narrower skip is only for an untrusted candidate
                    # manifest, where deleting on its strength is the worse
                    # outcome.)
                    log(f"WARN: skipping failed candidate {run_id}: "
                        f"untrusted manifest: {exc}")
                    continue
                if digest is None:
                    # Failed-build candidate: no manifest, no image. Nothing to
                    # collect — not an error, no rmi, no WARN, no audit event.
                    continue
                # The candidate payload (its config digest) is shared with a
                # live generation when it is pinned under ANY template or has a
                # promoted generation record under ANY template — a
                # cache-identical rebuild reuses the same image_id. In that case
                # the generation-retention path owns the payload; skip silently.
                if digest in pinned_digests or digest in promoted_digests:
                    continue
                deletions.append(_delete_candidate_payload(
                    layout, template, run_id, cdir, digest, dry_run, rmi,
                    image_exists))
    return deletions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-gc")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be deleted and why; delete nothing")
    args = parser.parse_args(argv)
    try:
        deletions = gc(dry_run=args.dry_run)
    except qt.TemplateError as exc:
        # Fail closed: a corrupt pin/binding/retention aborts before deleting.
        log(f"FATAL: aborting GC without deleting anything: {exc}")
        return 2
    except (ValueError, OSError) as exc:
        log(f"FATAL: aborting GC without deleting anything: {exc}")
        return 2
    if args.dry_run:
        log(f"(dry-run) {len(deletions)} payload(s) would be collected")
        return 0
    collected = sum(1 for d in deletions if d["deleted"])
    failed = len(deletions) - collected
    log(f"{collected} payload(s) collected"
        + (f", {failed} rmi failure(s)" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
