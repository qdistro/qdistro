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


def resolve(silo: str, layout: qt.Layout | None = None,
            record: bool = False) -> tuple[int, str | None]:
    layout = layout or qt.Layout()
    qt.require_safe_name(silo, "silo")
    binding_path = layout.binding_file(silo)
    if not os.path.isfile(binding_path):
        return 3, None
    # read_binding validates the schema, including: active_generation MUST
    # be an immutable digest. A tag reference raises here -> exit 2.
    binding = qt.read_binding(binding_path)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-resolve-binding")
    parser.add_argument("silo")
    parser.add_argument("--record", action="store_true",
                        help="record runtime status + activation transition")
    args = parser.parse_args(argv)
    try:
        rc, generation = resolve(args.silo, record=args.record)
    except qt.TemplateError as exc:
        log(f"FATAL: invalid binding for silo {args.silo!r}: {exc}")
        return 2
    if rc == 3:
        log(f"silo {args.silo!r} is untemplated (no binding)")
        return 3
    print(generation)
    return 0


if __name__ == "__main__":
    sys.exit(main())
