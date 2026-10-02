#!/usr/bin/env python3
"""Summarise test-vm-suites.sh output as Markdown; exit 2 if a suite is incomplete.

Completeness is checked against suites/expected (written before anything
runs), never inferred from the files that happen to exist:

  pytest  every process the guest listed in <suite>.tags finished (pytest
          exit 0 or 1) and wrote <tag>.xml;
  bats    the file's TAP exists, has a plan, and bats exited 0 or 1.

Assertion failures in a completed suite are results, not incompleteness.
"""
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

out = pathlib.Path(sys.argv[1])
junit = out / "junit"
expected = (out / "expected").read_text().splitlines() if (out / "expected").exists() else []
commit = next((l.split(" ", 1)[1] for l in expected if l.startswith("commit ")), "unknown")
want_pytest = [l.split(" ", 1)[1] for l in expected if l.startswith("pytest ")]
want_bats = [l.split(" ", 1)[1] for l in expected if l.startswith("bats ")]
incomplete = []

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
                problems.append(f"{tag}: " + ("never finished" if rc is None else f"pytest exit {rc}"))
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
        plan = next((int(m.group(1)) for l in text for m in [re.match(r"^1\.\.(\d+)", l)] if m), None)
        ok = sum(1 for l in text if re.match(r"^ok \d+ ", l) and "# skip" not in l)
        skip = sum(1 for l in text if re.match(r"^ok \d+ .*# skip", l))
        bad = [l for l in text if re.match(r"^not ok \d+ ", l)]
        rc = next((m.group(1) for l in text for m in [re.match(r"^# rc=(\d+)", l)] if m), None)
        problems = []
        if rc not in ("0", "1"):
            problems.append("never finished" if rc is None else
                            "timed out" if rc in ("124", "137") else f"bats exit {rc}")
        if plan is None:
            problems.append("no TAP plan")
        unrun = max(plan - ok - skip - len(bad), 0) if plan is not None else 0
        done = "yes" if not problems else "**no**"
        lines.append(f"| {base} | {done} | {plan if plan is not None else '?'} | {ok} | {len(bad)} | {skip} | {unrun} | {rc or '?'} |")
        for i, v in enumerate((plan or 0, ok, len(bad), skip, unrun)):
            btot[i] += v
        incomplete += [f"bats {base}: {p}" for p in problems]
        if bad:
            bats_failed[base] = [re.sub(r"^not ok \d+ ", "", l) for l in bad]
        n = [l[2:] for l in text if l.startswith("# harness:")]
        if n:
            notes[base] = n
    lines.append(f"| **total** | | {btot[0]} | {btot[1]} | {btot[2]} | {btot[3]} | {btot[4]} | |")

if incomplete:
    lines += ["", "### Incomplete", "",
              "These did not run to completion; their counts above are partial or missing.", ""]
    lines += [f"- {p}" for p in incomplete]
if notes:
    lines += ["", "### Harness restarts between bats files", ""]
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
