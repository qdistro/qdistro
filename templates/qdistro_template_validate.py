"""qdistro-template-validate — run a template's declared probes against a
built candidate in a minimal disposable runtime (todo/fableplan task 03).

    qdistro-template-validate <run-id>

Launches a disposable, throwaway-home container from the candidate's
immutable digest (``generation_ref``), runs each probe the template
declares, writes a per-check validation report into the candidate's
``evidence/``, and sets the candidate ``state`` to ``validated`` or
``failed``. It never touches a binding or any real silo state, and the
disposable container/home are removed afterward (evidence stays).

Probes are ``local-runtime`` class by default: ``--network=none`` is
enforced unless a probe explicitly declares ``class = "remote-read"``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import qdistro_templates as qt
import qdistro_template_audit as audit

# A compile-and-run probe: write a tiny C program, compile it with the
# toolchain in the candidate, run it, and require the sentinel on stdout.
# Proves gcc + libc + exec all work, not just that gcc --version prints.
_HELLO_SENTINEL = "hello-qdistro"
# Quoted heredoc so the shell passes the C source through verbatim — the
# program's own "\n" must reach the file intact (an unquoted printf would
# let the shell eat it and corrupt the string literal).
_HELLO_SCRIPT = (
    "set -e\n"
    "cat > /tmp/h.c <<'QDEOF'\n"
    "#include <stdio.h>\n"
    'int main(){printf("' + _HELLO_SENTINEL + '\\n");return 0;}\n'
    "QDEOF\n"
    "gcc /tmp/h.c -o /tmp/h\n"
    "/tmp/h\n"
)


def log(msg: str) -> None:
    print(f"[template-validate] {msg}", file=sys.stderr, flush=True)


def find_candidate(layout: qt.Layout, run_id: str) -> tuple[str, str] | None:
    """Locate (template, candidate_dir) for a run-id across all templates."""
    qt.require_safe_name(run_id, "run-id")
    root = layout.templates_var
    if not os.path.isdir(root):
        return None
    for template in os.listdir(root):
        cdir = os.path.join(root, template, "candidates", run_id)
        if os.path.isdir(cdir):
            return template, cdir
    return None


def _probe_argv(probe: dict) -> list[str]:
    kind = probe.get("kind")
    if kind == "process":
        return ["/bin/sh", "-c", probe.get("command", "true")]
    if kind == "command":
        if "command" not in probe:
            raise qt.TemplateError(f"probe {probe.get('name')!r}: command kind needs a command")
        return ["/bin/sh", "-c", probe["command"]]
    if kind == "compile-run":
        return ["/bin/sh", "-c", _HELLO_SCRIPT]
    if kind == "window":
        # Needs the nested-compositor disposable runtime (D-Bus, fonts, a
        # nested weston). Deferred — refuse rather than silently pass.
        raise qt.TemplateError(
            f"probe {probe.get('name')!r}: 'window' kind needs the nested "
            f"compositor runtime (deferred to a later slice)"
        )
    raise qt.TemplateError(f"probe {probe.get('name')!r}: unknown kind {kind!r}")


def run_probe(image_ref: str, probe: dict, container_name: str) -> dict:
    """Run one probe in a disposable container; return a CheckResult dict.

    The container is read-only with a tmpfs /tmp (the throwaway home), all
    caps dropped, no-new-privileges, and — for local-runtime probes —
    no network at all."""
    argv = _probe_argv(probe)
    timeout = int(probe.get("timeout", 60))
    remote = probe.get("class") == "remote-read"
    cmd = [
        "podman", "run", "--rm", "--name", container_name,
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--read-only", "--tmpfs", "/tmp:rw,exec,size=128m",
        "-e", "HOME=/tmp", "-e", "TMPDIR=/tmp", "-w", "/tmp",
    ]
    if not remote:
        cmd.append("--network=none")
    cmd += [image_ref] + argv

    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        duration = time.monotonic() - start
        output = (proc.stdout or "") + (proc.stderr or "")
        passed = proc.returncode == 0
        reason = ""
        if not passed:
            reason = f"exit {proc.returncode}: {output.strip()[-400:]}"
        elif probe.get("kind") == "compile-run" and _HELLO_SENTINEL not in output:
            passed = False
            reason = f"sentinel {_HELLO_SENTINEL!r} not in output"
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        passed = False
        reason = f"timed out after {timeout}s"
        subprocess.run(["podman", "rm", "-f", container_name],
                       capture_output=True, text=True)
    return {
        "name": probe.get("name", "unnamed"),
        "kind": probe.get("kind", "unknown"),
        "class": "remote-read" if remote else "local-runtime",
        "required": bool(probe.get("required", True)),
        "result": "pass" if passed else "fail",
        "duration_seconds": round(duration, 3),
        "reason": reason,
    }


def validate(run_id: str, layout: qt.Layout | None = None, runner=run_probe) -> int:
    layout = layout or qt.Layout()
    audit_db = os.path.join(layout.var, "audit", "template_audit.sqlite")

    def _refuse(reason: str, *, template=None, generation=None) -> int:
        audit.emit("template.validate.finished", db_path=audit_db,
                   template=template, run_id=run_id, generation=generation,
                   result="refused", reason=reason, duration=0.0)
        log(f"FATAL: {reason}")
        return 2

    found = find_candidate(layout, run_id)
    if found is None:
        return _refuse(f"no candidate with run-id {run_id}")
    template, cdir = found
    state = qt.candidate_state(cdir)
    if state != "built":
        return _refuse(f"candidate {run_id} is state={state!r}; only a 'built' "
                       f"candidate can be validated", template=template)

    manifest = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    if manifest.get("run_id") != run_id or manifest.get("template") != template:
        # The candidate dir and its manifest must agree on identity, or we
        # would validate the wrong digest under the wrong policy/evidence.
        return _refuse(f"candidate {run_id} manifest identity mismatch "
                       f"(run_id={manifest.get('run_id')!r} "
                       f"template={manifest.get('template')!r})", template=template)
    image_ref = qt.generation_ref(manifest)
    policy = qt.validate_template_policy(qt.read_toml(layout.template_policy(template)))
    probes = policy["template"].get("probe", [])
    if not probes:
        return _refuse(f"template {template} declares no probes",
                       template=template, generation=image_ref)

    evidence_dir = os.path.join(cdir, "evidence")
    os.makedirs(evidence_dir, exist_ok=True)
    log(f"validating run_id={run_id} template={template} image={image_ref} "
        f"({len(probes)} probes)")

    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wall_start = time.monotonic()
    checks = []
    for idx, probe in enumerate(probes):
        ctr = f"qdistro-validate-{run_id}-{idx}"
        try:
            check = runner(image_ref, probe, ctr)
        except Exception as exc:  # noqa: BLE001
            # An unsupported/malformed probe (window kind, missing command,
            # unknown kind) must surface as a failed check with evidence —
            # never abort the run and leave the candidate in limbo.
            check = {
                "name": probe.get("name", "unnamed"),
                "kind": probe.get("kind", "unknown"),
                "class": "remote-read" if probe.get("class") == "remote-read"
                         else "local-runtime",
                "required": bool(probe.get("required", True)),
                "result": "fail",
                "duration_seconds": 0.0,
                "reason": f"probe setup error: {exc}",
            }
        checks.append(check)
        log(f"  probe {check['name']}: {check['result']}"
            + (f" ({check['reason']})" if check["reason"] else ""))
    duration = round(time.monotonic() - wall_start, 3)

    failed_required = [c for c in checks if c["required"] and c["result"] == "fail"]
    result = "validated" if not failed_required else "failed"

    report = {
        "run_id": run_id,
        "template": template,
        "generation_ref": image_ref,
        "result": result,
        "started_at": started,
        "duration_seconds": duration,
        "checks_total": len(checks),
        "checks_failed": sum(1 for c in checks if c["result"] == "fail"),
        "check": checks,
    }
    qt.write_toml_atomic(os.path.join(evidence_dir, "validation.toml"), report, 0o644)

    # Record the validation result on the manifest too (validation command
    # + report pointer), then flip the candidate state.
    manifest["validation"] = {
        "command": "qdistro-template-validate",
        "result": result,
        "report": "evidence/validation.toml",
        "checks_total": len(checks),
        "checks_failed": report["checks_failed"],
        "validated_at": started,
    }
    qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"),
                         qt.validate_manifest(manifest), 0o644)
    qt.set_candidate_state(cdir, "validated" if result == "validated" else "failed")

    audit.emit("template.validate.finished", db_path=audit_db,
               template=template, run_id=run_id, generation=image_ref,
               result=result, duration=duration,
               reason=(f"{report['checks_failed']}/{report['checks_total']} checks failed"
                       if report["checks_failed"] else ""),
               evidence_path=evidence_dir,
               checks_total=report["checks_total"],
               checks_failed=report["checks_failed"])

    if result == "validated":
        log(f"OK: candidate {run_id} validated ({len(checks)} probes passed)")
        return 0
    log(f"FAIL: candidate {run_id} failed validation "
        f"({len(failed_required)} required checks failed); state=failed")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-validate")
    parser.add_argument("run_id", help="candidate run-id to validate")
    args = parser.parse_args(argv)
    try:
        return validate(args.run_id)
    except (qt.TemplateError, OSError) as exc:
        log(f"FATAL: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
