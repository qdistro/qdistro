"""qdistro-template-build — build a candidate generation for a derived
podman template (todo/fableplan task 02).

    qdistro-template-build <template> [--no-cache]

Builds the template's Containerfile with podman, resolves the result to
an immutable image digest immediately (never keeps a tag reference), and
records a candidate generation under

    /var/lib/qdistro/templates/<t>/candidates/<run-id>/

with the generation manifest, the build log, and a ``state`` marker. The
build is the "empty room": no secrets, no user state, no credentials are
passed into the build, so untrusted installer code (zypper post scripts)
runs with nothing to steal. No binding is ever touched by this tool — a
candidate is not a launch target until it is validated and promoted.

A failed build leaves its evidence (log + ``state = "failed"``) and exits
nonzero.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
import time

import qdistro_templates as qt
import qdistro_template_audit as audit

# Where shipped recipe Containerfiles live on the target. A policy's
# ``containerfile`` may be a bare name (resolved here) or an absolute path.
RECIPES_DIRS = (
    "/usr/lib/qdistro/templates/recipes",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipes"),
)

# The empty room is enforced, not assumed. podman is run with an explicit
# allowlisted environment so no host credential reaches the build: notably
# *_PROXY (podman's default --http-proxy copies HTTP(S)_PROXY — which often
# carries bearer creds — into RUN steps), SSH_AUTH_SOCK, and the various
# registry/buildah auth-file pointers are dropped. Combined with
# --http-proxy=false and passing no --build-arg / --env / mount, untrusted
# recipe + postinstall code runs with nothing to steal.
_ALLOWED_BUILD_ENV = (
    "PATH", "HOME", "USER", "LOGNAME",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TERM",
    "XDG_RUNTIME_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def _clean_env() -> dict:
    return {k: os.environ[k] for k in _ALLOWED_BUILD_ENV if k in os.environ}


def _audit_db(layout: qt.Layout) -> str:
    return os.path.join(layout.var, "audit", "template_audit.sqlite")


def log(msg: str) -> None:
    print(f"[template-build] {msg}", file=sys.stderr, flush=True)


def make_run_id() -> str:
    """Timestamped, collision-resistant candidate run id.

    The random suffix makes two builds in the same second distinct, which
    the acceptance test relies on."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{os.urandom(4).hex()}"


def resolve_containerfile(policy: dict) -> str:
    build = policy["template"].get("build")
    if not build or "containerfile" not in build:
        raise qt.TemplateError("[template.build].containerfile is required to build")
    ref = build["containerfile"]
    if os.path.isabs(ref):
        if not os.path.isfile(ref):
            raise qt.TemplateError(f"containerfile {ref} not found")
        return ref
    for base in RECIPES_DIRS:
        candidate = os.path.join(base, ref)
        if os.path.isfile(candidate):
            return candidate
    raise qt.TemplateError(
        f"containerfile {ref!r} not found under {RECIPES_DIRS}"
    )


def file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def declared_network_mode(policy: dict) -> str:
    mode = policy["template"].get("build", {}).get("network_mode", "unrestricted")
    if mode != "unrestricted":
        # record/replay need the content-addressed recording proxy, which
        # is a later slice. Refuse rather than silently running with plain
        # egress under a mode that claims to restrict it.
        raise qt.TemplateError(
            f"network_mode {mode!r} requires the recording proxy (deferred); "
            f"this slice supports only 'unrestricted'"
        )
    return mode


def write_candidate_manifest(candidate_dir: str, *, template: str, run_id: str,
                             image_digest: str, image_id: str,
                             containerfile: str, containerfile_digest: str,
                             build_command: str, network_mode: str) -> dict:
    """Assemble + validate + atomically write the candidate manifest."""
    manifest = {
        "template": template,
        "run_id": run_id,
        "image_digest": image_digest,
        "image_id": image_id,
        "containerfile_digest": containerfile_digest,
        "build_command": build_command,
        "network_mode": network_mode,
        # Fetched-artifact record. Empty until the recording proxy lands
        # (deferred); with unrestricted egress we cannot enumerate fetches,
        # which is exactly why network_mode stays honestly "unrestricted".
        "artifact_manifest": [],
        # The single canonical digest a silo launches and a binding pins.
        # image_id is podman's locally-resolvable config digest.
        "generation_ref": image_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "builder": "qdistro-template-build",
        # Input file hashes — the recipe inputs that produced this image.
        "inputs": [
            {"path": os.path.basename(containerfile), "sha256": containerfile_digest},
        ],
    }
    qt.validate_manifest(manifest)
    qt.write_toml_atomic(os.path.join(candidate_dir, "manifest.toml"), manifest, 0o644)
    return manifest


def _podman_inspect(tag: str, fmt: str) -> str:
    out = subprocess.run(
        ["podman", "inspect", "--format", fmt, tag],
        capture_output=True, text=True, check=True, env=_clean_env(),
    )
    return out.stdout.strip()


def _make_candidate_dir(layout: qt.Layout, template: str) -> tuple[str, str]:
    """Create a fresh candidate dir exclusively; never reuse/overwrite an
    existing one. Returns (run_id, candidate_dir)."""
    os.makedirs(layout.candidates_dir(template), exist_ok=True)
    for _ in range(8):
        run_id = make_run_id()
        candidate_dir = layout.candidate_dir(template, run_id)
        try:
            os.mkdir(candidate_dir)
            return run_id, candidate_dir
        except FileExistsError:
            continue
    raise qt.TemplateError("could not allocate a unique candidate run-id")


def _normalize_digest(value: str) -> str:
    """podman ``{{.Id}}`` is bare 64-hex (the config digest); ``{{.Digest}}``
    already carries the ``sha256:`` prefix. Normalise to the prefixed form
    so both are immutable digests we can launch and pin by."""
    return value if value.startswith("sha256:") else "sha256:" + value


def build(template: str, layout: qt.Layout | None = None,
          no_cache: bool = False) -> int:
    """Build a candidate; return the process exit code."""
    return build_candidate(template, layout=layout, no_cache=no_cache)[0]


def build_candidate(template: str, layout: qt.Layout | None = None,
                    no_cache: bool = False) -> tuple[int, str | None]:
    """Build a candidate; return (exit code, run_id). run_id is None when the
    build fails before a candidate dir is created. Callers (the freshness
    timer) need the exact run-id of the candidate they just built so they
    validate that one, never whatever dir happens to have the newest mtime."""
    layout = layout or qt.Layout()
    audit_db = _audit_db(layout)

    def _preflight_fail(reason: str, network_mode=None) -> tuple[int, None]:
        # A build attempt that fails before a candidate exists still carries
        # the audit contract: emit a failed build.finished.
        audit.emit("template.build.finished", db_path=audit_db,
                   template=template, result="failed", reason=reason,
                   network_mode=network_mode, duration=0.0)
        log(f"FATAL: {reason}")
        return 2, None

    policy_path = layout.template_policy(template)
    if not os.path.isfile(policy_path):
        return _preflight_fail(f"no policy at {policy_path}")
    policy = qt.validate_template_policy(qt.read_toml(policy_path))
    if policy["template"]["class"] != "derived":
        return _preflight_fail(f"template {template} is not 'derived'; build "
                               f"path is derived-only in this slice")
    try:
        containerfile = resolve_containerfile(policy)
        network_mode = declared_network_mode(policy)
    except qt.TemplateError as exc:
        return _preflight_fail(str(exc))

    run_id, candidate_dir = _make_candidate_dir(layout, template)
    log(f"template={template} run_id={run_id} containerfile={containerfile}")
    # Inputs hash is known before the build runs, so a failed build still
    # records the input identity that was attempted.
    containerfile_digest = file_digest(containerfile)
    audit.emit("template.build.started", db_path=audit_db, template=template,
               run_id=run_id, network_mode=network_mode,
               containerfile_digest=containerfile_digest)

    # Once the candidate dir exists, every failure must leave it marked
    # state=failed with its evidence — never an unmarked half-built dir.
    try:
        rc = _run_build(template, run_id, candidate_dir, containerfile,
                        network_mode, no_cache, audit_db)
        return rc, run_id
    except Exception as exc:  # noqa: BLE001 — record evidence, fail closed
        with open(os.path.join(candidate_dir, "build.log"), "a", encoding="utf-8") as logf:
            logf.write(f"\n[template-build] FATAL: {exc!r}\n")
        qt.set_candidate_state(candidate_dir, "failed")
        audit.emit("template.build.finished", db_path=audit_db, template=template,
                   run_id=run_id, result="failed", reason=str(exc),
                   evidence_path=candidate_dir)
        log(f"FAIL: {exc}; evidence in {candidate_dir} (state=failed)")
        return 1, run_id


def _run_build(template: str, run_id: str, candidate_dir: str,
               containerfile: str, network_mode: str, no_cache: bool,
               audit_db: str) -> int:
    started = time.monotonic()
    # Candidate tag is unique per run-id so it never shadows another
    # candidate; the durable reference is the digest we resolve below.
    tag = f"qdistro-candidate/{template}:{run_id}"
    # --http-proxy=false: do not copy host HTTP(S)_PROXY (often
    # credential-bearing) into the build's RUN steps. The clean env below
    # also drops them; this is belt-and-suspenders.
    cmd = ["podman", "build", "--http-proxy=false",
           "--file", containerfile, "--tag", tag]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(os.path.dirname(containerfile))
    build_command = shlex.join(cmd)

    log_path = os.path.join(candidate_dir, "build.log")
    with open(log_path, "w", encoding="utf-8") as logf:
        logf.write(f"$ {build_command}\n\n")
        logf.flush()
        proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                              env=_clean_env())

    if proc.returncode != 0:
        qt.set_candidate_state(candidate_dir, "failed")
        audit.emit("template.build.finished", db_path=audit_db, template=template,
                   run_id=run_id, result="failed", network_mode=network_mode,
                   reason=f"podman build exit {proc.returncode}",
                   duration=round(time.monotonic() - started, 3),
                   evidence_path=candidate_dir)
        log(f"FAIL: podman build returned {proc.returncode}; evidence in "
            f"{candidate_dir} (state=failed)")
        return 1

    image_digest = _normalize_digest(_podman_inspect(tag, "{{.Digest}}"))
    image_id = _normalize_digest(_podman_inspect(tag, "{{.Id}}"))
    containerfile_digest = file_digest(containerfile)
    qt.require_digest(image_id, "podman image id")
    qt.require_digest(image_digest, "podman image digest")

    write_candidate_manifest(
        candidate_dir, template=template, run_id=run_id,
        image_digest=image_digest, image_id=image_id,
        containerfile=containerfile, containerfile_digest=containerfile_digest,
        build_command=build_command, network_mode=network_mode,
    )
    qt.set_candidate_state(candidate_dir, "built")
    audit.emit("template.build.finished", db_path=audit_db, template=template,
               run_id=run_id, result="success", generation=image_id,
               network_mode=network_mode,
               duration=round(time.monotonic() - started, 3),
               evidence_path=candidate_dir)
    log(f"OK: candidate {run_id} built; generation_ref={image_id} state=built")
    # Machine-parseable lines for callers (validate/promote orchestration).
    print(f"RUN_ID={run_id}")
    print(f"GENERATION_REF={image_id}")
    print(f"IMAGE_DIGEST={image_digest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-build")
    parser.add_argument("template", help="template name (policy in /etc/qdistro/templates)")
    parser.add_argument("--no-cache", action="store_true",
                        help="build from scratch, ignoring the layer cache "
                             "(used by the freshness timer)")
    args = parser.parse_args(argv)
    try:
        return build(args.template, no_cache=args.no_cache)
    except qt.TemplateError as exc:
        log(f"FATAL: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
