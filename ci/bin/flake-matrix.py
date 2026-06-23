#!/usr/bin/env python3
"""Render a cross-run flake recurrence matrix from qci run artifacts.

Reads the `results.tsv` of the most recent N qci runs and prints, per scenario,
a row of single-character cells (newest run leftmost):

    .  pass        F  fail        s  skip        B  blocked        -  absent

This mechanically reproduces todo/testflakes/data-flake-recurrence-matrix.md so
the flake landscape can be regenerated on demand and per-scenario flake RATE is
visible (a scenario failing 1/8 runs reads as exactly that). It is reporting
only — it gates nothing and reads nothing but on-disk TSVs, so it is safe to run
while a qci full is live.

Usage:
    flake-matrix.py [--runs-dir DIR] [--limit N] [--pattern GLOB]
                    [--gate GATE] [--all] [--columns]

By default only scenarios that were ever non-pass (fail/blocked) across the
window are shown (the interesting ones); --all shows every scenario.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

CELL = {"pass": ".", "fail": "F", "skip": "s", "blocked": "B"}
ABSENT = "-"


def run_label(run_dir: Path) -> str:
    """Short, stable column label: the trailing token of the run id.

    qci run dirs are named `<gate>-<stamp>-<pid>` (e.g. full-20260623T...-1895720);
    the trailing pid disambiguates runs in the same second and matches the labels
    used in the hand-written matrix doc.
    """
    name = run_dir.name
    return name.rsplit("-", 1)[-1] if "-" in name else name


def read_results(path: Path, gate: str | None) -> dict[str, str]:
    """Map `<gate>/<subject>` -> status for one run's results.tsv."""
    out: dict[str, str] = {}
    try:
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                g = (row.get("gate") or "").strip()
                subject = (row.get("subject") or "").strip()
                status = (row.get("status") or "").strip()
                if not subject:
                    continue
                if gate and g != gate:
                    continue
                # Key by gate+subject so two gates with the same subject name do
                # not collide; gui scenarios are unique by subject already.
                key = f"{g}/{subject}" if not gate else subject
                out[key] = status
    except FileNotFoundError:
        pass
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs-dir", default=None,
                    help="qci runs dir (default: ci/runs relative to this script)")
    ap.add_argument("--limit", type=int, default=8, help="number of recent runs (default 8)")
    ap.add_argument("--pattern", default="full-*", help="run-dir glob (default full-*)")
    ap.add_argument("--gate", default=None, help="restrict to one gate (e.g. gui)")
    ap.add_argument("--all", action="store_true",
                    help="show every scenario, not just ever-non-pass ones")
    ap.add_argument("--columns", action="store_true",
                    help="also print a per-run fail/pass/skip column tally")
    args = ap.parse_args(argv)

    runs_dir = Path(args.runs_dir) if args.runs_dir else Path(__file__).resolve().parents[1] / "runs"
    if not runs_dir.is_dir():
        print(f"flake-matrix: runs dir not found: {runs_dir}", file=sys.stderr)
        return 2

    # Newest first. Run-dir names are timestamp-sorted, so a reverse name sort is
    # newest-first and deterministic (no mtime dependence).
    run_dirs = sorted((d for d in runs_dir.glob(args.pattern) if d.is_dir()),
                      key=lambda d: d.name, reverse=True)[: args.limit]
    if not run_dirs:
        print(f"flake-matrix: no runs match {args.pattern} in {runs_dir}", file=sys.stderr)
        return 2

    per_run = [read_results(d / "results.tsv", args.gate) for d in run_dirs]
    labels = [run_label(d) for d in run_dirs]

    scenarios = sorted({k for run in per_run for k in run})
    if not args.all:
        scenarios = [s for s in scenarios
                     if any(run.get(s) in ("fail", "blocked") for run in per_run)]
    if not scenarios:
        print("flake-matrix: no scenarios to show "
              "(no fails/blocks across the window; use --all)", file=sys.stderr)
        return 0

    width = max((len(s) for s in scenarios), default=8) + 2
    col_w = max((len(lbl) for lbl in labels), default=7) + 1

    header = "SCENARIO".ljust(width) + "".join(lbl.ljust(col_w) for lbl in labels)
    print(header)
    for s in scenarios:
        cells = "".join(CELL.get(run.get(s, ""), ABSENT).ljust(col_w) for run in per_run)
        print(s.ljust(width) + cells)

    if args.columns:
        print()
        print("run".ljust(col_w) + "pass".rjust(6) + "fail".rjust(6)
              + "skip".rjust(6) + "blocked".rjust(9))
        for lbl, run in zip(labels, per_run):
            tally: dict[str, int] = {}
            for st in run.values():
                tally[st] = tally.get(st, 0) + 1
            print(lbl.ljust(col_w) + str(tally.get("pass", 0)).rjust(6)
                  + str(tally.get("fail", 0)).rjust(6)
                  + str(tally.get("skip", 0)).rjust(6)
                  + str(tally.get("blocked", 0)).rjust(9))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
