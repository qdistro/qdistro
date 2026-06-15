"""Unit tests for ci/lib/report.py skip classification (04/F6).

dependency_missing_skips() separates a genuine missing-dependency skip (a bake
or host regression that must be surfaced) from a benign expected-environment
skip (no VM, no seat, headless display). The classifier is REPORT-ONLY — it
changes no pass/fail — but a misclassification either hides a real dependency
gap or cries wolf on every headless run. This pins _DEP_MISSING_RE to the
runners' actual dependency-missing vocabulary and, crucially, that benign
environment skips are NOT flagged.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ci" / "lib"))
import report  # noqa: E402


def _skip(notes):
    return {"status": "skip", "notes": notes, "subject": "x", "gate": "host"}


# Genuine dependency-missing skips — MUST be flagged. These are the live
# phrasings emitted by the gates/helpers/vm-probes (qci, helpers.bash, s56/s57/
# s59), not an idealised vocabulary.
DEP_MISSING = [
    "shellcheck not installed",
    "bats not installed",
    "missing shellcheck/bats",                 # qci gate
    "missing bats",
    "missing dependency: ruff",
    "optional tool missing; related gate will fail or skip",  # qci:616
    "no module named 'pytest_qt'",
    "No module named foo",
    "registry.tsv not found at /run/x",        # qci:1958
    "command not found: meson",
    "dbus-send absent",                        # s56-broker-enforcing (tool)
    "systemctl absent",                        # tool
    "busctl absent",                           # systemd dbus probe tool
    "systemd-run absent",
    "semodule absent",                         # SELinux probe tooling
    "sesearch absent",
    "dbus-python not importable",              # s57-qsu-argv-scopes
    "python3 needed for scan",                 # s59-tier2-podapps-discovery
    "TPM tooling not configured",
]

# Benign expected-environment skips — MUST NOT be flagged. These used to match
# the broad could-not / cannot / unavailable / not-set / bare-not-available
# terms; several are live qci env-skip rows (ctrl-socket / nested KVM).
ENV_SKIPS = [
    "could not connect to display",
    "cannot acquire wl_seat on headless backend",
    "wl_seat unavailable in headless weston",
    "DISPLAY not set",
    "no VM available; VM-gated",
    "legacy qdshell ctrl-socket not available",   # qci:1409
    "nested KVM not available in VM",             # qci:1424
    "headless: no seat",
    "requires AC power",
    "deselected: needs_ssh",
    "missing headings",          # a content skip, not a dependency
    "skipped: slow integration test",
    # State / device / opt-in "absent" rows — NOT dependency gaps (the subject
    # is a device/socket/image/disk, not a tool). Bare "absent" must not flag.
    "/dev/dri/renderD128 absent — VM has no virtio-gpu",   # s31-pixelfeed-dmabuf
    "ydotoold socket absent — /dev/uinput likely missing",  # s60-launcher
    "$IMAGE absent — run s32 first to build it",            # s34-tier2-lifecycle
    "tier-5 base disk absent (opt-in bake)",               # tiered-isolation.bats
    "profile absent",            # word-boundary: must not match via "file"
]


@pytest.mark.parametrize("notes", DEP_MISSING)
def test_dependency_missing_is_flagged(notes):
    rows = [_skip(notes)]
    assert report.dependency_missing_skips(rows) == rows, notes


@pytest.mark.parametrize("notes", ENV_SKIPS)
def test_environment_skip_is_not_flagged(notes):
    assert report.dependency_missing_skips([_skip(notes)]) == [], notes


def test_only_skip_rows_are_considered():
    # A failing row whose notes mention a missing dep is NOT a "skip" and must
    # not appear in the dependency-missing-SKIPS list.
    fail_row = {"status": "fail", "notes": "ruff not installed", "subject": "x"}
    assert report.dependency_missing_skips([fail_row]) == []


def test_mixed_rows_partition():
    rows = [
        _skip("shellcheck not installed"),
        _skip("could not connect to display"),
        {"status": "pass", "notes": "", "subject": "ok"},
        _skip("no module named 'gi'"),
    ]
    flagged = report.dependency_missing_skips(rows)
    assert [r["notes"] for r in flagged] == [
        "shellcheck not installed", "no module named 'gi'"]
