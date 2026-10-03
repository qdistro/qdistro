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
They are grouped at the bottom as unexpected regressions vs expected
minimal-image absences (the GitHub test VM omits optional subsystems; see
EXPECTED_ABSENT). Grouping is reporting only — it never changes a count or
the exit status.
"""
import fnmatch
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

# ---- what this image deliberately omits --------------------------------------
# The GitHub test VM is minimal on purpose. test-vm-guest-install.sh is its
# definition: the SKIP_STEPS default drops optional bootstrap-chain steps,
# the case filter drops runtime packages, pkgs+=() adds a few back and the
# pip loop installs only the shell apps; install-deps.sh holds the runtime
# package list the filter cuts. Classifying a failure as "expected" needs
# POSITIVE evidence from this profile, so the parse is all-or-nothing: a
# missing file or a pattern that fails to match leaves the profile unknown,
# every failure then classifies as unexpected and the caveat below says so —
# a half-parsed profile can never produce an "expected" label. (SKIP_STEPS
# is the script's default: qdistro-test-vm.yml runs the installer with no
# QDISTRO_TEST_VM_SKIP_STEPS override, so the parsed default is what ran.)
vm = pathlib.Path(__file__).parent
profile_err = []          # non-empty => the omissions are unknown
skipped_steps = set()     # bootstrap-chain steps the guest drops
drop_alts = []            # package globs the guest's case filter drops
added_pkgs = set()        # packages the guest adds after that filter
pip_apps = set()          # app dirs the guest's pip loop installs
runtime_pkgs = set()      # install-deps.sh's list after its own filter


def _profile_read(path):
    try:
        return path.read_text()
    except OSError:
        profile_err.append(f"{path.name} unreadable")
        return None


def _case_drops(src, var):
    """Globs of every `alt|alt) ;;` drop arm of `case "$var" in ... esac`."""
    m = re.search(rf'case "\${var}" in(.*?)esac', src, re.S)
    if not m:
        return None
    return [alt for arm in re.findall(
        r"^\s*([\w.*+-]+(?:\|[\w.*+-]+)*\) ;;)", m.group(1), re.M)
        for alt in arm[:-4].split("|")]


install_src = _profile_read(vm / "test-vm-guest-install.sh")
deps_src = _profile_read(vm / "install-deps.sh")
if install_src is not None:
    m = re.search(r"SKIP_STEPS=\$\{QDISTRO_TEST_VM_SKIP_STEPS:-([^}]*)\}",
                  install_src)
    if m:
        skipped_steps = set(m.group(1).split())
    else:
        profile_err.append("SKIP_STEPS default not found in "
                           "test-vm-guest-install.sh")
    alts = _case_drops(install_src, "pkg")
    if alts:
        drop_alts = alts
    else:
        profile_err.append("runtime-package case filter not found in "
                           "test-vm-guest-install.sh")
    # Part of the complete profile, never classification evidence on its
    # own: the prepared bats lane installs apps outside this loop (the
    # overlay installs qnotebook), so "not in pip_apps" proves nothing.
    m = re.search(r'for dir in ([^;\n]+);\s*do\s*\n\s*tvm_log "pip install',
                  install_src)
    if m:
        pip_apps = {p.rsplit("/", 1)[-1] for p in m.group(1).split()}
    else:
        profile_err.append("pip install loop not found in "
                           "test-vm-guest-install.sh")
    # Anchored at a line start: the case filter's `*) pkgs+=("$pkg") ;;` arm
    # also spells `pkgs+=(` — matching mid-line would capture shell tokens.
    m = re.search(r"^\s*pkgs\+=\((.*?)\)\s*$", install_src, re.S | re.M)
    if m:
        added_pkgs = set(re.sub(r"#.*", "", m.group(1)).split())
    else:
        profile_err.append("pkgs+=() additions not found in "
                           "test-vm-guest-install.sh")
if deps_src is not None:
    m = re.search(r"^QDISTRO_PKGS=\(\n(.*?)^\)", deps_src, re.S | re.M)
    alts = _case_drops(deps_src, "_pkg")
    if m and alts:
        runtime_pkgs = {p for p in re.sub(r"#.*", "", m.group(1)).split()
                        if not any(fnmatch.fnmatchcase(p, a) for a in alts)}
    else:
        profile_err.append("runtime package list not found in "
                           "install-deps.sh")
profile_ok = not profile_err
# The guest's effective package set: the runtime list minus the case-filter
# drops plus the pkgs+=() additions (qemu-guest-agent is added back — it is
# the guest-agent transport, not a hypervisor).
effective_pkgs = ({p for p in runtime_pkgs
                   if not any(fnmatch.fnmatchcase(p, a) for a in drop_alts)}
                  | added_pkgs) if profile_ok else set()


def feature_absent(feature):
    """Is FEATURE positively evidenced as one of this image's omissions?

    With an incomplete profile nothing is absent — unknown or missing
    profile information can never excuse a failure.
    """
    if not profile_ok:
        return False
    if feature in skipped_steps:
        return True
    if feature == "browser":            # no bridge, no browser
        return "browser-bridge" in skipped_steps
    if (any(fnmatch.fnmatchcase(feature, a) for a in drop_alts)
            and feature not in added_pkgs):
        return True                     # filtered out and not added back
    if feature == "xwayland":           # the effective runtime set provably
        return not any(p == "xwayland" or p.startswith("xwayland-")
                       for p in effective_pkgs)     # carries no Xwayland
    if feature == "kiwi-base":          # clone-baseweed needs libvirt + QEMU;
        virt = ("libvirt", "virt-install", "libguestfs", "guestfs-tools")
        return (not any(p == v or p.startswith(v + "-")
                        for p in effective_pkgs for v in virt)
                and all(p == "qemu-guest-agent" for p in effective_pkgs
                        if p.startswith("qemu")))
    return False


# bats file -> [(test-name prefix or None, (features,), note)]. A failed test
# is an expected absence when ANY listed feature is positively evidenced as
# omitted on this image. Prefixes stay as narrow as the failing tests; None
# means the whole file targets the absent feature. Note names what the test
# cannot reach. There is deliberately no app:<name> feature: the prepared
# bats lane installs apps beyond the image's pip loop (the overlay installs
# qnotebook before bats runs), so omission from that loop proves nothing —
# and neither presentation-four-apps (a host-side probe importing the
# checkout) nor the qnotebook send-to cases map here.
EXPECTED_ABSENT = {
    # The four 9e desktop-integration daemons install with the bridge.
    "browser-9e-daemons": [
        (None, ("browser-bridge",), "9e desktop-integration daemons")],
    "pwd-print-recall": [
        ("phase9-print-", ("print",), "print-VM helpers / allowlist surfaces"),
        ("phase8-browser-bridge-probe", ("browser-bridge",), "bridge surfaces"),
        # The probe drives the bridge to check recall.push stays unregistered.
        ("v1-recall-cut-probe", ("browser-bridge",), "bridge (recall.push check)"),
        ("phase8-snapshots-probe", ("snapshots",), "snapshot/backup surfaces"),
        ("phase8-phone-probe", ("phone",), "phone daemon + CLI")],
    # clone-baseweed checks the libvirt template BEFORE the kiwi base; the
    # guest's effective package set provably has no libvirt or QEMU.
    "kiwi-ci-base": [
        ("clone-baseweed: --from-kiwi", ("kiwi-base",), "libvirt template VM")],
    # The probe checks print-proxy is active and the Xwayland binary exists.
    "gui-fixes-verify": [
        ("gui-fixes:", ("print", "xwayland"), "print-proxy / Xwayland")],
    # Needs /dev/uinput for ydotool's synthetic input: the Minimal-VM cloud
    # kernel is kernel-default-base (no CONFIG_INPUT_UINPUT) and the package
    # filter drops kernel-default.
    "tiered-isolation": [
        ("phase7-tier2-launcher-click", ("kernel-default",), "/dev/uinput")],
}


def expected_absence(base, name):
    """The omission note when bats failure NAME in FILE is an expected
    minimal-image absence, else None."""
    for prefix, features, note in EXPECTED_ABSENT.get(base, ()):
        if prefix is None or name.startswith(prefix):
            if any(feature_absent(f) for f in features):
                return note
    return None


phases = next((l.split(" ", 1)[1] for l in expected if l.startswith("phases ")), "")
bats_filter = next((l.split(" ", 1)[1] for l in expected if l.startswith("bats-filter ")), "")
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
# The failure-triage headline is inserted here once the counts are known.
triage_at = len(lines)

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
        plan = next((int(m.group(1)) for l in text for m in [re.match(r"^1\.\.(\d+)", l)] if m), None)
        results = [m for l in text for m in [re.match(r"^(ok|not ok) (\d+) (.*)$", l)] if m]
        setup_failed = any(m.group(1) == "not ok" and m.group(3).startswith("setup_file failed") for m in results)
        teardown_failed = [m for m in results if m.group(1) == "not ok" and m.group(3).startswith("teardown_file failed")]
        tests = [m for m in results if m not in teardown_failed
                 and not (m.group(1) == "not ok" and m.group(3).startswith("setup_file failed"))]
        ok = sum(1 for m in tests if m.group(1) == "ok" and "# skip" not in m.group(3))
        skip = sum(1 for m in tests if m.group(1) == "ok" and "# skip" in m.group(3))
        bad = [l for l in text if re.match(r"^not ok \d+ ", l)]
        rc = next((m.group(1) for l in text for m in [re.match(r"^# rc=(\d+)", l)] if m), None)
        problems = []
        if rc not in ("0", "1"):
            problems.append("never finished" if rc is None else
                            "timed out" if rc in ("124", "137") else f"bats exit {rc}")
        if plan is None:
            problems.append("no TAP plan")
        if any(l.startswith("Bail out!") for l in text):
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
            bats_failed[base] = [re.sub(r"^not ok \d+ ", "", l) for l in bad]
        n = [l[2:] for l in text if l.startswith("# harness:")]
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
# ---- failure triage -----------------------------------------------------------
# Group the failures — nothing here changes a count or the exit status:
# expected-minimal-image-absence (the test probes a subsystem the image
# omits by design; tests fail loudly on missing deps, see tests/AGENTS.md)
# vs unexpected-regression (no documented omission explains it — triage).
unexpected_cases, expected_cases = {}, {}
for name, cases in failed_cases.items():
    unexpected_cases[f"pytest {name}"] = [f"`{c}`" for c in cases[:200]]
for base, cases in bats_failed.items():
    for c in cases:
        note = expected_absence(base, c)
        if note:
            expected_cases.setdefault(f"bats {base}", []).append(f"{c}  *(absent: {note})*")
        else:
            unexpected_cases.setdefault(f"bats {base}", []).append(c)
n_unexp = sum(len(v) for v in unexpected_cases.values())
n_exp = sum(len(v) for v in expected_cases.values())
if n_unexp or n_exp:
    triage = [
        "### Failure triage", "",
        f"{n_unexp + n_exp} failed tests: **{n_unexp} unexpected regressions**, "
        f"{n_exp} expected on the minimal image (they probe subsystems the "
        "image omits by design; details at the bottom). This grouping is "
        "reporting only — failed tests still count as failed."]
    if not profile_ok:
        triage += ["", "> The image-omission profile could not be parsed "
                   f"({'; '.join(profile_err)}), so the expected-absence "
                   "classification is unavailable and every failure below "
                   "is listed as unexpected."]
    triage.append("")
    lines[triage_at:triage_at] = triage
if unexpected_cases:
    lines += ["", "### Unexpected regressions", "",
              "No documented minimal-image omission explains these — triage them.", ""]
    for name, cases in unexpected_cases.items():
        lines.append(f"**{name}**")
        lines += [f"- {c}" for c in cases]
if expected_cases:
    lines += ["", f"<details><summary>Expected on the minimal image ({n_exp} failed "
              "tests — subsystems the image omits by design)</summary>", "",
              "What the image omits is derived live: "
              "`scripts/vm/test-vm-guest-install.sh` (the SKIP_STEPS "
              "default, the package case filter and additions, the pip app "
              "set) applied to `scripts/vm/install-deps.sh`'s runtime list — "
              "positive evidence only, so if any piece fails to parse no "
              "absence is claimed at all. An omission that stops being "
              "omitted turns its tests back into unexpected regressions.", ""]
    for name, cases in expected_cases.items():
        lines.append(f"**{name}**")
        lines += [f"- {c}" for c in cases]
    lines += ["", "</details>"]
print("\n".join(lines))
sys.exit(2 if incomplete or not expected else 0)
