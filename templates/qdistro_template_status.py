"""qdistro-template-status — machine-readable template/silo running status
(fableplan2 task 04).

    qdistro-template-status [--json]

Scoped to what the browser-rollback demo needs to observe; no human
formatting in this slice (KEY=VALUE by default, --json for structure). For
each templated silo it reports, from the on-disk model only (no daemon):

  - bound generation      (binding.active_generation)
  - running generation    (/run/qdistro/silo-generation/<silo>, written by
                           qdistro-resolve-binding --record at launch)
  - restart_pending       true when bound != running (a promote landed but
                           the silo has not restarted onto it yet)
  - parked validated candidates per template, with their validation result
  - rollback targets       (binding.previous_generations) with pin expiry

Sources: bindings, the per-boot runtime status files, candidate manifests +
validation reports, and pin receipts. Read-only; never mutates anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import qdistro_templates as qt

RUN_STATUS_DIR = os.environ.get("QDISTRO_RUN_STATUS_DIR",
                                "/run/qdistro/silo-generation")


def _running_generation(silo: str, run_status_dir: str) -> str | None:
    path = os.path.join(run_status_dir, silo)
    if not os.path.isfile(path):
        return None
    try:
        data = qt.read_toml(path)
    except (OSError, ValueError):
        return None
    gen = data.get("generation")
    return gen if isinstance(gen, str) else None


def _rollback_targets(layout: qt.Layout, template: str,
                      previous: list[str]) -> list[dict]:
    out = []
    for gen in previous:
        pin_path = os.path.join(layout.pins_for(template, gen),
                                "rollback-window.toml")
        expires = None
        if os.path.isfile(pin_path):
            try:
                expires = qt.read_toml(pin_path).get("expires_at")
            except (OSError, ValueError):
                expires = None
        out.append({
            "generation": gen,
            "pin_expires_at": expires,
            "image_present": os.path.isdir(layout.generation_dir(template, gen)),
        })
    return out


def _parked_candidates(layout: qt.Layout, template: str) -> list[dict]:
    cdir_root = layout.candidates_dir(template)
    if not os.path.isdir(cdir_root):
        return []
    out = []
    for run_id in sorted(os.listdir(cdir_root)):
        cdir = os.path.join(cdir_root, run_id)
        state = qt.candidate_state(cdir)
        if state != "validated":
            continue
        result = None
        report = os.path.join(cdir, "evidence", "validation.toml")
        if os.path.isfile(report):
            try:
                result = qt.read_toml(report).get("result")
            except (OSError, ValueError):
                result = None
        out.append({"run_id": run_id, "validation_result": result})
    return out


def collect(layout: qt.Layout | None = None,
            run_status_dir: str | None = None) -> dict:
    layout = layout or qt.Layout()
    # Read the env at call time (not import time) so a CLI invocation / test
    # override is honoured.
    run_status_dir = run_status_dir or os.environ.get(
        "QDISTRO_RUN_STATUS_DIR", "/run/qdistro/silo-generation")
    silos = []
    bindings_dir = layout.bindings_dir
    if os.path.isdir(bindings_dir):
        for fname in sorted(os.listdir(bindings_dir)):
            if not fname.endswith(".toml"):
                continue
            silo = fname[:-5]
            try:
                binding = qt.read_binding(os.path.join(bindings_dir, fname))
            except (qt.TemplateError, OSError, ValueError):
                # A malformed binding is surfaced as an error row, never
                # silently dropped — status must not hide a broken binding.
                silos.append({"silo": silo, "error": "unreadable-binding"})
                continue
            template = binding["template"]
            bound = binding["active_generation"]
            running = _running_generation(silo, run_status_dir)
            silos.append({
                "silo": silo,
                "template": template,
                "bound_generation": bound,
                "running_generation": running,
                "restart_pending": running is not None and running != bound,
                "rollback_targets": _rollback_targets(
                    layout, template, binding.get("previous_generations", [])),
                "parked_candidates": _parked_candidates(layout, template),
            })
    return {"silos": silos}


def _emit_keyvalue(status: dict) -> None:
    # Strictly KEY=VALUE: every line starts with a record= type and every
    # value is a single whitespace-free token (so a `for kv: k,v=split('=')`
    # parser never trips). No bare words, no spaces inside a value.
    for s in status["silos"]:
        silo = s["silo"]
        if "error" in s:
            print(f"record=silo silo={silo} error={s['error']}")
            continue
        print(f"record=silo silo={silo} template={s['template']} "
              f"bound_generation={s['bound_generation']} "
              f"running_generation={s['running_generation'] or '-'} "
              f"restart_pending={'yes' if s['restart_pending'] else 'no'}")
        for t in s["rollback_targets"]:
            print(f"record=rollback_target silo={silo} "
                  f"generation={t['generation']} "
                  f"pin_expires_at={t['pin_expires_at'] or '-'} "
                  f"image_present={'yes' if t['image_present'] else 'no'}")
        for c in s["parked_candidates"]:
            print(f"record=parked_candidate silo={silo} run_id={c['run_id']} "
                  f"validation_result={c['validation_result'] or '-'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-status")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of KEY=VALUE lines")
    args = parser.parse_args(argv)
    try:
        status = collect()
    except OSError as exc:
        print(f"[template-status] FATAL: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        _emit_keyvalue(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
