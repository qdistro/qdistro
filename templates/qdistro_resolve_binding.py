"""qdistro-resolve-binding — resolve a silo's launch target from its
template binding (todo/fableplan task 05).

    qdistro-resolve-binding <silo> [--record]

This is the enforcement point for "a candidate is mechanically unable to
launch against real state". It reads /var/lib/qdistro/bindings/<silo>.toml
and prints the silo's active_generation **digest** to stdout. Because
read_binding validates the binding schema, a non-digest reference (a
mutable tag) is a hard error — there is no tag fallback. Candidates never
appear in bindings (only qdistro-template-promote writes them), so the
only image a silo can launch from is a promoted generation.

Exit codes:
  0  resolved — the active_generation digest is on stdout
  2  the binding exists but is invalid (e.g. a tag reference) — hard error
  3  no binding — the silo is untemplated; the caller keeps legacy behaviour

With --record, the resolved generation is written to a per-boot status
file (/run/qdistro/silo-generation/<silo>) so the admin UI and journal can
see which generation a running silo actually uses, and a binding
activation transition (resolved generation differs from the last recorded
one) is reported on stderr — the anchor for the template.binding.activated
audit event wired in task 06.

With --launch-env (fableplan2 task 01), the launch path's full input set
(GENERATION/TEMPLATE/STATE_PATH/FIRST_ACTIVATION) is emitted as KEY=VALUE
lines from a SINGLE binding read, so spawn-tier2 mounts real state without
parsing TOML in bash and without a second binding read that could race a
concurrent promote.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import qdistro_templates as qt
import qdistro_template_audit as audit

RUN_STATUS_DIR = os.environ.get("QDISTRO_RUN_STATUS_DIR",
                                "/run/qdistro/silo-generation")


def log(msg: str) -> None:
    print(f"[resolve-binding] {msg}", file=sys.stderr, flush=True)


def activated_marker(layout: qt.Layout, silo: str) -> str:
    # Persistent record of the last generation actually activated for this
    # silo (survives reboot), kept next to the binding.
    return os.path.join(layout.bindings_dir, f"{silo}.activated")


def read_activated_marker(layout: qt.Layout, silo: str) -> str | None:
    """The last generation recorded as activated for this silo, or None.

    Read-only: computing FIRST_ACTIVATION must not commit the marker (task
    05 splits the marker commit out so a failed pre-activation snapshot
    leaves the obligation un-discharged)."""
    marker = activated_marker(layout, silo)
    if not os.path.isfile(marker):
        return None
    with open(marker, encoding="utf-8") as fh:
        return fh.read().strip() or None


def record_activation(layout: qt.Layout, silo: str, generation: str,
                      run_status_dir: str | None = None) -> bool:
    """Record the running generation and report whether this is a new
    activation (resolved generation differs from the last recorded one).

    Returns True when the activation changed (caller emits
    template.binding.activated)."""
    # Read the module default at call time so it stays overridable.
    run_status_dir = run_status_dir or RUN_STATUS_DIR
    # Per-boot runtime status: which generation is running right now.
    os.makedirs(run_status_dir, exist_ok=True)
    qt.atomic_write(
        os.path.join(run_status_dir, silo),
        f"generation = {generation!r}\nresolved_at = "
        f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())!r}\n",
        0o644,
    )
    marker = activated_marker(layout, silo)
    last = None
    if os.path.isfile(marker):
        with open(marker, encoding="utf-8") as fh:
            last = fh.read().strip()
    changed = last != generation
    if changed:
        qt.atomic_write(marker, generation + "\n", 0o600)
    return changed


def _read_resolved_binding(silo: str, layout: qt.Layout) -> tuple[int, dict | None]:
    """Single binding read + generation-record validation.

    Returns (3, None) for an absent binding (untemplated) and (0, binding)
    when the active_generation resolves to a promoted generation record.
    Raises TemplateError/OSError for an invalid or unreadable binding — the
    hard-error path with no tag fallback. This is the one place the binding
    is read, so the launch path never reads it twice (a second read could
    race a concurrent promote)."""
    qt.require_safe_name(silo, "silo")
    binding_path = layout.binding_file(silo)
    # Only a genuinely absent binding maps to rc 3 (untemplated). An
    # unreadable binding (EACCES on the file or its dir, EIO, ...) must be a
    # hard error: os.path.isfile() swallows those into False and would
    # fail-open to the mutable :latest tag at the launch boundary.
    # read_binding validates the schema, including: active_generation MUST
    # be an immutable digest. A tag reference raises TemplateError here -> exit 2.
    try:
        binding = qt.read_binding(binding_path)
    except FileNotFoundError:
        return 3, None
    generation = binding["active_generation"]

    # A digest alone is not enough: the launch target must be a *promoted
    # generation*, not merely any local sha256 image. A corrupt or
    # hand-authored binding pointing at a parked candidate digest must be
    # refused, or the candidate-isolation boundary would leak. Require the
    # generation record (materialized only by qdistro-template-promote) and
    # that its manifest agrees on the digest.
    gen_dir = layout.generation_dir(binding["template"], generation)
    man_path = os.path.join(gen_dir, "manifest.toml")
    if not os.path.isfile(man_path):
        raise qt.TemplateError(
            f"active_generation {generation} has no promoted generation "
            f"record at {gen_dir} — refusing to launch an unpromoted image")
    if qt.generation_ref(qt.read_manifest(man_path)) != generation:
        raise qt.TemplateError(
            f"generation record at {gen_dir} does not match "
            f"active_generation {generation}")
    return 0, binding


def compute_launch_env(silo: str, layout: qt.Layout | None = None) -> tuple[int, dict | None]:
    """Everything the launch path needs from a SINGLE binding read, with NO
    side effects (no run-status file, no marker, no audit).

    Returns (3, None) for an untemplated silo and (0, env) otherwise, where
    env has ``generation``, ``template``, ``state_path``, and
    ``first_activation`` (True when the resolved generation differs from the
    last activated marker — read, never written, here). The caller decides
    when to commit the marker (task 05: only after the pre-activation
    snapshot succeeds)."""
    layout = layout or qt.Layout()
    rc, binding = _read_resolved_binding(silo, layout)
    if rc == 3:
        return 3, None
    generation = binding["active_generation"]
    return 0, {
        "generation": generation,
        "template": binding["template"],
        "state_path": binding["state_path"],
        "first_activation": read_activated_marker(layout, silo) != generation,
    }


def resolve(silo: str, layout: qt.Layout | None = None,
            record: bool = False) -> tuple[int, str | None]:
    layout = layout or qt.Layout()
    rc, binding = _read_resolved_binding(silo, layout)
    if rc == 3:
        return 3, None
    generation = binding["active_generation"]

    if record:
        # Status recording is advisory — never let a non-writable /run break
        # the launch. The digest resolution above is the load-bearing part.
        try:
            changed = record_activation(layout, silo, generation)
            if changed:
                # The new generation is actually starting now — this is the
                # anchor the first-activation state snapshot (deferred) hangs
                # off of.
                audit.emit("template.binding.activated",
                           db_path=os.path.join(layout.var, "audit",
                                                "template_audit.sqlite"),
                           silo=silo, template=binding["template"],
                           generation=generation, result="activated")
                log(f"binding.activated silo={silo} generation={generation}")
            else:
                log(f"silo={silo} already running generation={generation}")
        except OSError as exc:
            log(f"WARN: could not record activation status for {silo}: {exc}")
    return 0, generation


def _launch_env_main(silo: str, layout: qt.Layout, record: bool) -> int:
    """`--launch-env`: emit KEY=VALUE lines for the launch path from ONE
    binding read, so spawn-tier2 never parses TOML in bash nor reads the
    binding twice. With --record, the per-boot status + marker + audit are
    committed under the current ordering (task 05 moves the marker commit to
    after the pre-activation snapshot)."""
    rc, env = compute_launch_env(silo, layout)
    if rc == 3:
        log(f"silo {silo!r} is untemplated (no binding)")
        return 3
    if record:
        try:
            changed = record_activation(layout, silo, env["generation"])
            if changed:
                audit.emit("template.binding.activated",
                           db_path=os.path.join(layout.var, "audit",
                                                "template_audit.sqlite"),
                           silo=silo, template=env["template"],
                           generation=env["generation"], result="activated")
                log(f"binding.activated silo={silo} generation={env['generation']}")
        except OSError as exc:
            log(f"WARN: could not record activation status for {silo}: {exc}")
    print(f"GENERATION={env['generation']}")
    print(f"TEMPLATE={env['template']}")
    print(f"STATE_PATH={env['state_path']}")
    print(f"FIRST_ACTIVATION={'yes' if env['first_activation'] else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-resolve-binding")
    parser.add_argument("silo")
    parser.add_argument("--record", action="store_true",
                        help="record runtime status + activation transition")
    parser.add_argument("--launch-env", action="store_true",
                        help="emit GENERATION/TEMPLATE/STATE_PATH/"
                             "FIRST_ACTIVATION KEY=VALUE lines from a single "
                             "binding read (for spawn-tier2)")
    args = parser.parse_args(argv)
    layout = qt.Layout()
    try:
        if args.launch_env:
            return _launch_env_main(args.silo, layout, args.record)
        rc, generation = resolve(args.silo, layout=layout, record=args.record)
    except (qt.TemplateError, OSError) as exc:
        # OSError covers an unreadable binding/generation record (EACCES, EIO):
        # those are hard errors at the launch boundary, not a tag fallback and
        # not a crash traceback. (Status-recording OSErrors are caught inside
        # resolve() and stay advisory — they never reach here.)
        log(f"FATAL: invalid binding for silo {args.silo!r}: {exc}")
        return 2
    if rc == 3:
        log(f"silo {args.silo!r} is untemplated (no binding)")
        return 3
    print(generation)
    return 0


if __name__ == "__main__":
    sys.exit(main())
