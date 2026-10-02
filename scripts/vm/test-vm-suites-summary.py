#!/usr/bin/env python3
"""Summarise test-vm-suites.sh output (junit XML + bats TAP) as Markdown."""
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

out = pathlib.Path(sys.argv[1])
lines = ["## Test suites in the test VM", ""]

suites = {}
failed_cases = {}
for xml in sorted((out / "junit").glob("*.xml")):
    name = re.sub(r"-\d+$", "", xml.stem)
    s = suites.setdefault(name, dict(tests=0, failures=0, errors=0, skipped=0, batches=0))
    s["batches"] += 1
    root = ET.parse(xml).getroot()
    for ts in root.iter("testsuite"):
        for k in ("tests", "failures", "errors", "skipped"):
            s[k] += int(ts.get(k, 0))
    for tc in root.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            failed_cases.setdefault(name, []).append(f'{tc.get("classname")}::{tc.get("name")}')
for log in sorted(out.glob("pytest-*.log")):
    suites.setdefault(log.stem[len("pytest-"):], dict(tests=0, failures=0, errors=0, skipped=0, batches=0))

lines += ["### pytest (inside the guest)", "",
          "| suite | tests | passed | failed | errors | skipped |", "|---|---:|---:|---:|---:|---:|"]
tot = dict(tests=0, failures=0, errors=0, skipped=0)
for name, s in suites.items():
    passed = s["tests"] - s["failures"] - s["errors"] - s["skipped"]
    note = "" if s["batches"] else " (no junit: see pytest log)"
    lines.append(f'| {name}{note} | {s["tests"]} | {passed} | {s["failures"]} | {s["errors"]} | {s["skipped"]} |')
    for k in tot:
        tot[k] += s[k]
tp = tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"]
lines.append(f'| **total** | {tot["tests"]} | {tp} | {tot["failures"]} | {tot["errors"]} | {tot["skipped"]} |')

lines += ["", "### bats (tests/integration/vm, over SSH)", "",
          "| file | ok | failed | skipped | rc |", "|---|---:|---:|---:|---:|"]
btot = [0, 0, 0]
bats_failed = {}
for tap in sorted((out / "bats").glob("*.tap")):
    text = tap.read_text(errors="replace").splitlines()
    ok = sum(1 for l in text if re.match(r"^ok \d+ ", l) and "# skip" not in l)
    skip = sum(1 for l in text if re.match(r"^ok \d+ .*# skip", l))
    bad = [l for l in text if re.match(r"^not ok \d+ ", l)]
    rc = next((m.group(1) for l in text for m in [re.match(r"^# rc=(\d+)", l)] if m), "?")
    lines.append(f"| {tap.stem} | {ok} | {len(bad)} | {skip} | {rc} |")
    btot[0] += ok; btot[1] += len(bad); btot[2] += skip
    if bad:
        bats_failed[tap.stem] = [re.sub(r"^not ok \d+ ", "", l) for l in bad]
lines.append(f"| **total** | {btot[0]} | {btot[1]} | {btot[2]} | |")
not_run = sorted((out / "bats").glob("*.not-run"))
if not_run:
    lines += ["", "Not run (the test VM leaves this out on purpose):", ""]
    lines += [f"- {p.stem}: {p.read_text().strip()}" for p in not_run]

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
