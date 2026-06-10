"""qdistro-template-promote — the gated, manual binding flip
(todo/fableplan task 04).

    qdistro-template-promote <silo> <run-id>
    qdistro-template-promote <silo> --rollback <generation-digest>

Promotion is owner-initiated. It refuses anything but a validated
candidate, re-resolves the silo's app-identity selectors against the
candidate (an identity *class* change fails closed before anything is
written), materializes a promoted generation record, writes pin receipts,
and atomically rewrites the binding. The binding write is the last
mutating step and is atomic, so a crash leaves the binding either fully
old or fully new — never partial; any extra pin receipts or generation
record left by a mid-run crash are harmless.

The running silo is untouched; the new generation takes effect at the
silo's next restart.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

import qdistro_templates as qt
import qdistro_template_audit as audit

# How long the outgoing generation stays pinned as a rollback target.
ROLLBACK_WINDOW_DAYS = int(os.environ.get("QDISTRO_ROLLBACK_WINDOW_DAYS", "14"))


def log(msg: str) -> None:
    print(f"[template-promote] {msg}", file=sys.stderr, flush=True)


def _audit_db(layout: qt.Layout) -> str:
    return os.path.join(layout.var, "audit", "template_audit.sqlite")


def _image_exists(digest: str) -> bool:
    return subprocess.run(["podman", "image", "exists", digest],
                          capture_output=True).returncode == 0


def _refuse(layout: qt.Layout, reason: str, *, rc: int = 1, silo=None,
            template=None, generation=None, run_id=None) -> int:
    audit.emit("template.promote.refused", db_path=_audit_db(layout),
               silo=silo, template=template, generation=generation,
               run_id=run_id, result="refused", reason=reason)
    log(f"REFUSED: {reason}")
    return rc


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --------------------------------------------------------------------------
# app-identity selector re-resolution
# --------------------------------------------------------------------------

def _selector_probe_script(path_in_template: str) -> str:
    # path_in_template is shell-quoted; the candidate runs it read-only with
    # no network, but a malformed selector must not be able to falsify the
    # output and weaken the class-change gate.
    q = shlex.quote(path_in_template)
    return (
        f'p=$(readlink -f {q} 2>/dev/null || true); '
        f'if [ -z "$p" ] || [ ! -e "$p" ]; then echo "PRESENT=no"; exit 0; fi; '
        f'echo "PRESENT=yes"; '
        f'echo "RESOLVED=$p"; '
        f'echo "SHA=$(sha256sum "$p" 2>/dev/null | cut -d\' \' -f1)"; '
        f'echo "PKG=$(rpm -qf --qf \'%{{NAME}}\' "$p" 2>/dev/null || echo unknown)"; '
        f'if [ -L {q} ]; then echo "WRAPPER=yes"; else echo "WRAPPER=no"; fi'
    )


def resolve_selector(image_ref: str, selector: dict) -> dict:
    """Resolve an identity selector inside the candidate image.

    KNOWN GAP (this slice): the probe runs inside the candidate, so a
    poisoned candidate could forge its output to slip a class change past
    this gate. That is acceptable here because promotion is manual and the
    untrusted-source audit gate is deferred (doc/templates.md); the robust
    fix is host-side inspection (podman mount/cp + host rpm --root), cheap on
    the podman-image backend, and is left to the audit-gate slice.

    Returns the executable's resolved path, content digest, owning package,
    whether the declared path is a symlink (wrapper), and the declared
    SELinux type carried from the selector (the runtime label is not a
    property of the image, so it is compared across generations, not read
    from the image). Runs read-only with no network."""
    exe = selector.get("executable", {})
    path_in_template = exe.get("path_in_template")
    if not path_in_template:
        raise qt.TemplateError("identity selector missing executable.path_in_template")
    out = subprocess.run(
        ["podman", "run", "--rm", "--network=none", "--read-only",
         "--tmpfs", "/tmp", image_ref, "/bin/sh", "-c",
         _selector_probe_script(path_in_template)],
        capture_output=True, text=True, timeout=60,
    )
    fields = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k] = v
    present = fields.get("PRESENT") == "yes"
    sha = fields.get("SHA", "")
    return {
        "present": present,
        "resolved_path": fields.get("RESOLVED", ""),
        "executable_digest": ("sha256:" + sha) if sha else "",
        "package": fields.get("PKG", "unknown"),
        "is_wrapper": fields.get("WRAPPER") == "yes",
        "selinux_type": exe.get("selinux_type", ""),
    }


def selector_class_change(selector: dict, resolved: dict,
                          prior: dict | None = None) -> str | None:
    """Return a reason string if the identity *class* changed, else None.

    Routine updates (same package, same path shape, same label) revalidate
    automatically. A different package name, a missing executable, the
    declared executable becoming a wrapper (symlink) without opt-in, or a
    SELinux-type change versus the previously promoted generation is a class
    change → fail closed."""
    exe = selector.get("executable", {})
    if not resolved["present"]:
        return f"executable {exe.get('path_in_template')!r} absent in candidate"
    expected_pkg = exe.get("expected_package")
    if expected_pkg and resolved["package"] != expected_pkg:
        return (f"package class changed: expected {expected_pkg!r}, "
                f"candidate has {resolved['package']!r}")
    if resolved.get("is_wrapper") and not exe.get("allow_wrapper"):
        return (f"executable {exe.get('path_in_template')!r} became a wrapper "
                f"(symlink) — set allow_wrapper to opt in")
    # SELinux runtime type is not an image attribute; detect a change by
    # comparing the declared type to the prior promoted generation's record.
    if prior is not None:
        prior_type = prior.get("selinux_type", "")
        if prior_type and prior_type != resolved.get("selinux_type", ""):
            return (f"SELinux type changed: was {prior_type!r}, "
                    f"now {resolved.get('selinux_type', '')!r}")
    return None


def _app_selectors(layout: qt.Layout, silo: str) -> list[tuple[str, dict]]:
    idir = layout.identity_for(silo)
    if not os.path.isdir(idir):
        return []
    out = []
    for name in sorted(os.listdir(idir)):
        if name.endswith(".toml"):
            # Selector files are `[identity.executable]` (doc/templates.md);
            # hand the inner `identity` table to the resolver.
            data = qt.read_toml(os.path.join(idir, name))
            out.append((name[:-5], data.get("identity", {})))
    return out


def _prior_identity_record(layout: qt.Layout, template: str,
                           outgoing: str | None, app: str) -> dict | None:
    if not outgoing:
        return None
    path = os.path.join(layout.generation_dir(template, outgoing),
                        "evidence", f"identity-{app}.toml")
    if os.path.isfile(path):
        return qt.read_toml(path)
    return None


def revalidate_identity(layout: qt.Layout, silo: str, template: str,
                        image_ref: str, outgoing: str | None,
                        resolver) -> list[dict]:
    """Re-resolve every app selector; abort (raise) on a class change.

    Compares against the previously promoted generation's recorded identity
    (for the SELinux-type check) and returns the per-app resolution records
    to store as generation evidence."""
    records = []
    for app, selector in _app_selectors(layout, silo):
        resolved = resolver(image_ref, selector)
        prior = _prior_identity_record(layout, template, outgoing, app)
        change = selector_class_change(selector, resolved, prior)
        if change is not None:
            raise qt.TemplateError(
                f"identity class change for app {app!r}: {change} — "
                f"failing closed, binding not flipped")
        records.append({
            "app": app,
            "resolved_path": resolved["resolved_path"],
            "executable_digest": resolved["executable_digest"],
            "package": resolved["package"],
            "is_wrapper": resolved["is_wrapper"],
            "selinux_type": resolved.get("selinux_type", ""),
        })
    return records


# --------------------------------------------------------------------------
# generation record + pins
# --------------------------------------------------------------------------

def _atomic_copy(src: str, dst: str, mode: int = 0o644) -> None:
    """Copy src to dst atomically (temp + fsync + rename) so a crash never
    leaves a partially written destination."""
    with open(src, "rb") as fh:
        qt.atomic_write_bytes(dst, fh.read(), mode)


def _manifest_already_materialized(man_dst: str) -> bool:
    """True only if a *complete, valid* manifest is already present, so a
    partial copy from a prior crash is re-done rather than trusted."""
    if not os.path.isfile(man_dst):
        return False
    try:
        qt.read_manifest(man_dst)
        return True
    except (qt.TemplateError, ValueError, OSError):
        return False


def materialize_generation(layout: qt.Layout, template: str, gen: str,
                           candidate_dir: str | None,
                           identity_records: list[dict]) -> str:
    """Create generations/<gen>/ with the manifest + evidence so bindings,
    pins, retention, and rollback reference a generation record — never a
    candidate dir. Crash-idempotent: every file is copied atomically, and a
    partial manifest from a prior crash is re-copied, not trusted."""
    gen_dir = layout.generation_dir(template, gen)
    evidence_dir = os.path.join(gen_dir, "evidence")
    os.makedirs(evidence_dir, exist_ok=True)
    if candidate_dir is not None:
        man_dst = os.path.join(gen_dir, "manifest.toml")
        if not _manifest_already_materialized(man_dst):
            _atomic_copy(os.path.join(candidate_dir, "manifest.toml"), man_dst)
        cand_evidence = os.path.join(candidate_dir, "evidence")
        if os.path.isdir(cand_evidence):
            for name in os.listdir(cand_evidence):
                _atomic_copy(os.path.join(cand_evidence, name),
                             os.path.join(evidence_dir, name))
    for rec in identity_records:
        qt.write_toml_atomic(
            os.path.join(evidence_dir, f"identity-{rec['app']}.toml"), rec, 0o644)
    return gen_dir


def write_active_pin(layout: qt.Layout, template: str, gen: str, silo: str,
                     now: float) -> None:
    pin = {
        "owner_type": "silo", "owner_id": silo, "reason": "active",
        "generation": gen, "template": template, "created_at": _iso(now),
    }
    path = os.path.join(layout.pins_for(template, gen), "active.toml")
    qt.write_pin(path, pin)


def write_rollback_pin(layout: qt.Layout, template: str, gen: str, silo: str,
                       now: float) -> None:
    pin = {
        "owner_type": "silo", "owner_id": silo, "reason": "rollback-window",
        "generation": gen, "template": template, "created_at": _iso(now),
        "expires_at": _iso(now + ROLLBACK_WINDOW_DAYS * 86400),
    }
    path = os.path.join(layout.pins_for(template, gen), "rollback-window.toml")
    qt.write_pin(path, pin)


def _remove_pin(layout: qt.Layout, template: str, gen: str, reason: str) -> None:
    path = os.path.join(layout.pins_for(template, gen), f"{reason}.toml")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# promote / rollback
# --------------------------------------------------------------------------

def _retained_previous_count(layout: qt.Layout) -> int:
    """How many rollback targets to keep in the binding. Mirrors the
    promoted-generation retention count. An absent file falls back to the
    default; a present-but-corrupt file raises (same fail-closed discipline
    as GC) rather than silently defaulting."""
    if not os.path.isfile(layout.retention_file):
        return 3
    retention = qt.validate_retention(qt.read_toml(layout.retention_file))
    return max(1, retention["keep_promoted_generations"])


def _build_binding(layout: qt.Layout, silo: str, template: str, new_gen: str,
                   outgoing: str | None, existing: dict | None,
                   identity_revision: int, state_path: str) -> dict:
    prev = list(existing["previous_generations"]) if existing else []
    if outgoing and outgoing != new_gen:
        prev = [outgoing] + [g for g in prev if g != outgoing]
    # The generation we are activating must not also appear as a rollback
    # target (matters for the rollback flip).
    prev = [g for g in prev if g != new_gen]
    # Drop rollback targets whose generation record no longer exists (GC'd
    # past their window) so the binding never advertises a dead target, and
    # cap the list to the retention count.
    prev = [g for g in prev if os.path.isdir(layout.generation_dir(template, g))]
    prev = prev[:max(0, _retained_previous_count(layout) - 1)]
    return {
        "silo": silo,
        "template": template,
        "backend": "podman-image",
        "active_generation": new_gen,
        "previous_generations": prev,
        "state_path": state_path,
        "activation_policy": existing["activation_policy"] if existing else "manual",
        "identity_revision": identity_revision,
    }


def promote(silo: str, run_id: str | None = None, *,
            rollback: str | None = None, layout: qt.Layout | None = None,
            resolver=resolve_selector, state_path: str | None = None,
            now: float | None = None, image_exists=_image_exists) -> int:
    layout = layout or qt.Layout()
    qt.require_safe_name(silo, "silo")
    now = time.time() if now is None else now
    binding_path = layout.binding_file(silo)
    existing = qt.read_binding(binding_path) if os.path.isfile(binding_path) else None

    if rollback is not None:
        return _do_rollback(layout, silo, rollback, existing, resolver, now,
                            image_exists)

    # --- promote a validated candidate ---------------------------------
    found = _find_candidate(layout, run_id)
    if found is None:
        # Generation is unknowable without a candidate; record the attempt.
        return _refuse(layout, f"no candidate with run-id {run_id}", rc=2,
                       silo=silo, run_id=run_id)
    template, cdir = found
    state = qt.candidate_state(cdir)
    if state != "validated":
        return _refuse(layout, f"candidate {run_id} is state={state!r}; only a "
                       f"'validated' candidate can be promoted",
                       silo=silo, template=template, run_id=run_id)

    manifest = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    new_gen = qt.generation_ref(manifest)

    # Now the full context is known — record the promotion request with the
    # old (outgoing) and new generation and the current identity revision.
    audit.emit("template.promote.requested", db_path=_audit_db(layout),
               silo=silo, template=template, run_id=run_id, generation=new_gen,
               new_generation=new_gen,
               old_generation=existing["active_generation"] if existing else None,
               identity_revision=existing["identity_revision"] if existing else None,
               result="requested")

    # The validation report is mandatory and must belong to THIS candidate —
    # a forged/stale state marker with no (or a mismatched) report must not
    # become promotable.
    report_path = os.path.join(cdir, "evidence", "validation.toml")
    if not os.path.isfile(report_path):
        return _refuse(layout, f"candidate {run_id} has no validation report "
                       f"(state marker alone is not sufficient)",
                       silo=silo, template=template, generation=new_gen, run_id=run_id)
    report = qt.read_toml(report_path)
    if (report.get("run_id") != manifest["run_id"]
            or report.get("template") != template
            or report.get("generation_ref") != new_gen):
        return _refuse(layout, f"validation report for {run_id} does not match "
                       f"the candidate manifest (run_id/template/generation_ref)",
                       silo=silo, template=template, generation=new_gen, run_id=run_id)
    failed_required = [c for c in report.get("check", [])
                       if c.get("required") and c.get("result") == "fail"]
    if report.get("result") != "validated" or failed_required:
        return _refuse(layout, f"validation report for {run_id} is not clean "
                       f"(result={report.get('result')!r}, "
                       f"{len(failed_required)} failed required checks)",
                       silo=silo, template=template, generation=new_gen, run_id=run_id)

    if existing and existing["template"] != template:
        return _refuse(layout, f"silo {silo} is bound to template "
                       f"{existing['template']!r}, candidate is {template!r}",
                       silo=silo, template=template, generation=new_gen, run_id=run_id)

    resolved_state_path = (
        state_path or (existing["state_path"] if existing else None)
        or f"/var/lib/qdistro/silos/{silo}/state"
    )
    return _apply(layout, silo, template, new_gen, cdir, existing, resolver,
                  resolved_state_path, now, mode="promote", run_id=run_id)


def _do_rollback(layout: qt.Layout, silo: str, target: str, existing: dict | None,
                 resolver, now: float, image_exists) -> int:
    qt.require_digest(target, "rollback generation")
    if existing is None:
        return _refuse(layout, f"silo {silo} has no binding to roll back",
                       silo=silo, generation=target)
    if target not in existing["previous_generations"]:
        return _refuse(layout, f"{target} is not in {silo}'s previous_generations",
                       silo=silo, template=existing["template"], generation=target)
    template = existing["template"]
    gen_dir = layout.generation_dir(template, target)
    if not os.path.isdir(gen_dir):
        return _refuse(layout, f"no generation record at {gen_dir} for rollback target",
                       silo=silo, template=template, generation=target)
    # Evidence outlives payload: the generation record can survive a GC that
    # already reclaimed the image. Refuse a rollback to an unlaunchable digest
    # rather than flipping the binding to a target the silo can't start.
    if not image_exists(target):
        return _refuse(layout, f"rollback target {target} has no image payload "
                       f"(collected past its rollback window); cannot launch it",
                       silo=silo, template=template, generation=target)
    audit.emit("template.promote.requested", db_path=_audit_db(layout),
               silo=silo, template=template, generation=target,
               new_generation=target, old_generation=existing["active_generation"],
               identity_revision=existing["identity_revision"],
               result="requested", reason="rollback")
    return _apply(layout, silo, template, target, None, existing, resolver,
                  existing["state_path"], now, mode="rollback")


def _apply(layout: qt.Layout, silo: str, template: str, new_gen: str,
           candidate_dir: str | None, existing: dict | None, resolver,
           state_path: str, now: float, mode: str, run_id: str | None = None) -> int:
    outgoing = existing["active_generation"] if existing else None
    if outgoing == new_gen:
        return _refuse(layout, f"{new_gen} is already the active generation "
                       f"for {silo}", silo=silo, template=template,
                       generation=new_gen, run_id=run_id)

    # Step 2: identity revalidation — fail closed BEFORE any pin/binding
    # write so a class change never leaves partial state.
    try:
        identity_records = revalidate_identity(
            layout, silo, template, new_gen, outgoing, resolver)
    except qt.TemplateError as exc:
        return _refuse(layout, str(exc), silo=silo, template=template,
                       generation=new_gen, run_id=run_id)
    identity_revision = (existing["identity_revision"] if existing else 0) + 1

    # Step 3: materialize the promoted generation record.
    materialize_generation(layout, template, new_gen, candidate_dir, identity_records)

    # Step 4: pins — ADDITIVE ONLY before the commit. The new generation is
    # pinned active and the outgoing one gets a rollback-window pin; we do
    # NOT remove any pin here. That guarantees the crash-consistency claim:
    # if we die before the binding write, both the (still-active) outgoing
    # generation and the new one are protected by an unexpired receipt, so
    # GC can never collect a live launch target.
    write_active_pin(layout, template, new_gen, silo, now)
    if outgoing:
        write_rollback_pin(layout, template, outgoing, silo, now)

    # Step 5: atomic binding rewrite — the single commit point.
    binding = _build_binding(layout, silo, template, new_gen, outgoing, existing,
                             identity_revision, state_path)
    qt.write_binding(layout.binding_file(silo), binding)

    # Post-commit, best-effort pin cleanup. A crash here leaves a stale pin
    # (an `active` pin whose generation no longer matches the binding, or a
    # redundant rollback-window pin on the now-active generation); GC treats
    # an `active` pin that disagrees with the binding as stale, so these
    # extras never wrongly protect or expose a generation.
    if outgoing:
        _remove_pin(layout, template, outgoing, "active")
    _remove_pin(layout, template, new_gen, "rollback-window")

    # Step 6: audit. template.promote.applied fires here (the flip happened);
    # template.binding.activated fires later in the launch path (task 05) when
    # the new generation first actually starts. Notify identity consumers via
    # the audit event (cache hint), not a correctness mechanism.
    audit.emit("template.promote.applied", db_path=_audit_db(layout),
               silo=silo, template=template, generation=new_gen, run_id=run_id,
               new_generation=new_gen, old_generation=outgoing,
               identity_revision=identity_revision, result="applied", reason=mode,
               evidence_path=os.path.join(layout.generation_dir(template, new_gen),
                                          "evidence"))
    verb = "rolled back to" if mode == "rollback" else "promoted"
    log(f"OK: {silo} {verb} generation {new_gen} "
        f"(identity_revision={identity_revision}); outgoing={outgoing}")
    log("The running silo is UNTOUCHED; the new generation takes effect at "
        "the silo's next restart.")
    print(f"SILO={silo}")
    print(f"ACTIVE_GENERATION={new_gen}")
    print(f"IDENTITY_REVISION={identity_revision}")
    return 0


def _find_candidate(layout: qt.Layout, run_id: str | None):
    if run_id is None:
        return None
    qt.require_safe_name(run_id, "run-id")
    root = layout.templates_var
    if not os.path.isdir(root):
        return None
    for template in os.listdir(root):
        cdir = os.path.join(root, template, "candidates", run_id)
        if os.path.isdir(cdir):
            return template, cdir
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-promote")
    parser.add_argument("silo")
    parser.add_argument("run_id", nargs="?", default=None,
                        help="validated candidate run-id to promote")
    parser.add_argument("--rollback", metavar="GENERATION_DIGEST", default=None,
                        help="flip back to a generation in previous_generations")
    parser.add_argument("--state-path", default=None,
                        help="silo state mount (first promote only; default "
                             "/var/lib/qdistro/silos/<silo>/state)")
    args = parser.parse_args(argv)
    if args.rollback is None and args.run_id is None:
        parser.error("a run-id or --rollback <digest> is required")
    if args.rollback is not None and args.run_id is not None:
        parser.error("give a run-id OR --rollback <digest>, not both")
    try:
        return promote(args.silo, args.run_id, rollback=args.rollback,
                       state_path=args.state_path)
    except qt.TemplateError as exc:
        log(f"FATAL: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
