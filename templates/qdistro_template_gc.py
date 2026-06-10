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
        if not name.endswith(".toml"):
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
                if not pin_name.endswith(".toml"):
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


def _rmi(image_ref: str) -> bool:
    proc = subprocess.run(["podman", "rmi", image_ref],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"WARN: podman rmi {image_ref} failed: {proc.stderr.strip()}")
        return False
    return True


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


def _delete_candidate_payload(layout: qt.Layout, template: str, run_id: str,
                              cdir: str, dry_run: bool, rmi) -> dict:
    evidence = cdir  # the candidate dir IS the evidence (build.log, manifest, report)
    tag = f"qdistro-candidate/{template}:{run_id}"
    deletion = {"kind": "candidate", "template": template, "run_id": run_id,
                "reason": "failed-candidate-expired", "evidence_path": evidence,
                "deleted": False}
    if dry_run:
        log(f"DRY-RUN would delete failed candidate payload {run_id} "
            f"(image {tag}); evidence kept at {evidence}")
        return deletion
    deletion["deleted"] = rmi(tag)
    if not deletion["deleted"]:
        log(f"NOT deleted: failed candidate payload {run_id} (podman rmi failed)")
        return deletion
    audit.emit("template.gc.deleted", db_path=_audit_db(layout),
               template=template, run_id=run_id, result="deleted",
               reason="failed-candidate-expired", evidence_path=evidence)
    log(f"deleted failed candidate payload {run_id}; evidence kept at {evidence}")
    return deletion


def gc(layout: qt.Layout | None = None, *, dry_run: bool = False,
       now: float | None = None, rmi=_rmi) -> list[dict]:
    layout = layout or qt.Layout()
    now = time.time() if now is None else now
    retention = load_retention(layout)            # fail-closed
    pinned = build_pinned_set(layout, now)        # fail-closed
    deletions: list[dict] = []
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
                deletions.append(_delete_candidate_payload(
                    layout, template, run_id, cdir, dry_run, rmi))
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
