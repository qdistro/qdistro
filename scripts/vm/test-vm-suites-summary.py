#!/usr/bin/env python3
"""Summarise test-vm-suites.sh output as Markdown; exit 2 if a suite is incomplete.

Completeness is checked against suites/expected (written before anything
runs), never inferred from the files that happen to exist:

  pytest  every process the guest listed in <suite>.tags finished (pytest
          exit 0 or 1) and wrote <tag>.xml;
  bats    the file's TAP exists, has a plan, bats exited 0 or 1, did not
          bail out, numbered its results 1..n, and every planned test has a
          result or is accounted for by a reported setup_file failure (bats
          then reports one "setup_file failed" record and runs nothing else;
          that is a failure of the file, counted as such, as qci counts it).

Assertion failures in a completed suite are results, not incompleteness.
"""
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

out = pathlib.Path(sys.argv[1])
junit = out / "junit"
expected = (out / "expected").read_text().splitlines() if (out / "expected").exists() else []
commit = next((line.split(" ", 1)[1] for line in expected if line.startswith("commit ")), "unknown")
want_pytest = [line.split(" ", 1)[1] for line in expected if line.startswith("pytest ")]
want_bats = [line.split(" ", 1)[1] for line in expected if line.startswith("bats ")]
incomplete = []

phases = next((line.split(" ", 1)[1] for line in expected if line.startswith("phases ")), "")
bats_filter = next((line.split(" ", 1)[1] for line in expected if line.startswith("bats-filter ")), "")
lines = [
    "## Test suites in the qdistro test VM", "",
    f"Commit `{commit}`. The shipped image booted through a throwaway overlay.",
    "Before the suites, the harness changed that overlay (none of this is in the",
    "image): test-only packages, a copy of the source tree for pytest and, before",
    "bats, what qci's test lane adds to the bootstrap chain (media, multimachine",
    "and template installers, `/etc/qdistro/profile` = dev, the approvals CLI, the",
    "in-VM probes, the RDP certificate, the fixed test password). admin logs in at",
    "the greeter first. bats runs from the runner through qci's `vm-exec` and the",
    "guest agent. All bats files share one guest in order (qci gives each its",
    "own VM); restarts the harness made between files are listed below.", "",
    f"Phases: {phases or 'unknown'}." + (f" bats limited to: {bats_filter}." if bats_filter else ""),
    "Not in this pytest selection: qdbrowser (QtWebEngine; no browser in the image)",
    "and qdterm's `tests/test_print_terminal.py` (the host gate excludes it too).", "",
]

# ---- pytest -----------------------------------------------------------------
if want_pytest:
    lines += ["### pytest (inside the guest, as admin)", "",
              "| suite | complete | tests | passed | failed | errors | skipped |",
              "|---|---|---:|---:|---:|---:|---:|"]
    tot = dict(tests=0, failures=0, errors=0, skipped=0)
    failed_cases = {}
    for name in want_pytest:
        s = dict(tests=0, failures=0, errors=0, skipped=0)
        problems = []
        tags_file = junit / f"{name}.tags"
        tags = tags_file.read_text().split() if tags_file.exists() else []
        if not tags:
            problems.append("did not start")
        for tag in tags:
            rc_file, xml = junit / f"{tag}.rc", junit / f"{tag}.xml"
            rc = rc_file.read_text().strip() if rc_file.exists() else None
            if rc not in ("0", "1"):
                problems.append(f"{tag}: " + ("never finished" if rc is None else
                                "stopped by the time budget" if rc == "budget" else
                                "timed out" if rc in ("124", "137") else f"pytest exit {rc}"))
            if not xml.exists():
                if rc in ("0", "1"):
                    problems.append(f"{tag}: no junit")
                continue
            try:
                root = ET.parse(xml).getroot()
            except ET.ParseError:
                problems.append(f"{tag}: unreadable junit")
                continue
            for ts in root.iter("testsuite"):
                for k in s:
                    s[k] += int(ts.get(k, 0))
            for tc in root.iter("testcase"):
                if tc.find("failure") is not None or tc.find("error") is not None:
                    failed_cases.setdefault(name, []).append(f'{tc.get("classname")}::{tc.get("name")}')
        passed = s["tests"] - s["failures"] - s["errors"] - s["skipped"]
        done = "yes" if not problems else "**no**"
        lines.append(f'| {name} | {done} | {s["tests"]} | {passed} | {s["failures"]} | {s["errors"]} | {s["skipped"]} |')
        for k in tot:
            tot[k] += s[k]
        incomplete += [f"pytest {name}: {p}" for p in problems]
    tp = tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"]
    lines.append(f'| **total** | | {tot["tests"]} | {tp} | {tot["failures"]} | {tot["errors"]} | {tot["skipped"]} |')
else:
    failed_cases = {}

# ---- bats -------------------------------------------------------------------
bats_failed, notes = {}, {}
if want_bats:
    lines += ["", "### bats (tests/integration/vm, through vm-exec)", "",
              "`not executed`: planned tests a failed `setup_file` kept from running.", "",
              "| file | complete | planned | ok | failed | skipped | not executed | rc |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    btot = [0, 0, 0, 0, 0]
    for base in want_bats:
        tap = out / "bats" / f"{base}.tap"
        if not tap.exists():
            lines.append(f"| {base} | **no** | | | | | | |")
            incomplete.append(f"bats {base}: did not run")
            continue
        text = tap.read_text(errors="replace").splitlines()
        plan = next((int(m.group(1)) for line in text for m in [re.match(r"^1\.\.(\d+)", line)] if m), None)
        results = [m for line in text for m in [re.match(r"^(ok|not ok) (\d+) (.*)$", line)] if m]
        setup_failed = any(m.group(1) == "not ok" and m.group(3).startswith("setup_file failed") for m in results)
        teardown_failed = [m for m in results if m.group(1) == "not ok" and m.group(3).startswith("teardown_file failed")]
        tests = [m for m in results if m not in teardown_failed
                 and not (m.group(1) == "not ok" and m.group(3).startswith("setup_file failed"))]
        ok = sum(1 for m in tests if m.group(1) == "ok" and "# skip" not in m.group(3))
        skip = sum(1 for m in tests if m.group(1) == "ok" and "# skip" in m.group(3))
        bad = [line for line in text if re.match(r"^not ok \d+ ", line)]
        rc = next((m.group(1) for line in text for m in [re.match(r"^# rc=(\d+)", line)] if m), None)
        problems = []
        if rc not in ("0", "1"):
            problems.append("never finished" if rc is None else
                            "timed out" if rc in ("124", "137") else f"bats exit {rc}")
        if plan is None:
            problems.append("no TAP plan")
        if any(line.startswith("Bail out!") for line in text):
            problems.append("bailed out")
        if [int(m.group(2)) for m in results] != list(range(1, len(results) + 1)):
            problems.append("results not numbered 1..n")
        unrun = 0
        if plan is not None:
            missing = plan - len(tests)
            if setup_failed and not tests:
                unrun = plan
            elif missing > 0:
                problems.append(f"{missing} planned tests have no result")
            elif missing < 0:
                problems.append(f"{-missing} results beyond the plan")
        done = "yes" if not problems else "**no**"
        lines.append(f"| {base} | {done} | {plan if plan is not None else '?'} | {ok} | {len(bad)} | {skip} | {unrun} | {rc or '?'} |")
        if teardown_failed:
            notes.setdefault(base, []).append("harness: teardown_file failed (counted in failed)")
        for i, v in enumerate((plan or 0, ok, len(bad), skip, unrun)):
            btot[i] += v
        incomplete += [f"bats {base}: {p}" for p in problems]
        if bad:
            bats_failed[base] = [re.sub(r"^not ok \d+ ", "", line) for line in bad]
        n = [line[2:] for line in text if line.startswith("# harness:")]
        if n:
            notes.setdefault(base, []).extend(n)
    lines.append(f"| **total** | | {btot[0]} | {btot[1]} | {btot[2]} | {btot[3]} | {btot[4]} | |")

if (out / "baseline-failed").exists():
    incomplete.append(f"bats stopped after {(out / 'baseline-failed').read_text().strip()}")
if incomplete:
    lines += ["", "### Incomplete", "",
              "These did not run to completion; their counts above are partial or missing.", ""]
    lines += [f"- {p}" for p in incomplete]
if notes:
    lines += ["", "### Harness notes between bats files", ""]
    for base, n in notes.items():
        lines += [f"- after {base}: {x.removeprefix('harness: ')}" for x in n]
if failed_cases or bats_failed:
    lines += ["", "<details><summary>Failed cases</summary>", ""]
    for name, cases in failed_cases.items():
        lines.append(f"**pytest {name}**")
        lines += [f"- `{c}`" for c in cases[:200]]
    for name, cases in bats_failed.items():
        lines.append(f"**bats {name}**")
        lines += [f"- {c}" for c in cases]
    lines += ["", "</details>"]
print("\n".join(lines))
sys.exit(2 if incomplete or not expected else 0)
