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


# --- nonactionable_failure_reason / partition_failures (Phase-1 clean-run) ---
# REPORT-ONLY: separates expected/operator FAIL/BLOCKED rows (release pins
# absent, operator interrupt, policy gate) from actionable product/test/infra
# failures. A misclassification either hides a real failure (bad) or lets an
# expected row keep the run permanently red (the bug this fixes).


def _row(**kw):
    base = {"status": "fail", "gate": "gui", "subject": "s", "notes": ""}
    base.update(kw)
    return base


def test_release_manifest_blocked_is_nonactionable():
    r = _row(gate="release-manifest", subject="unpopulated", status="blocked",
             notes="manifest unpopulated — no release pins")
    assert report.nonactionable_failure_reason(r) is not None


def test_operator_interrupt_is_nonactionable():
    for subj in ("INT", "TERM"):
        r = _row(gate="lifecycle", subject=subj, notes="run interrupted")
        assert report.nonactionable_failure_reason(r) is not None
    # matched via notes even if the subject differs
    assert report.nonactionable_failure_reason(
        _row(gate="lifecycle", subject="SIGHUP", notes="run interrupted")) is not None


def test_edit_guard_stays_actionable():
    # An unsanctioned protected-path edit is deterministic but human-required;
    # bucketing it would hide a future regression where a test/agent path starts
    # touching protected files. It must remain in the actionable count.
    r = _row(gate="edit-guard", subject="changed-paths",
             notes="2 protected edits not sanctioned")
    assert report.nonactionable_failure_reason(r) is None


def test_lifecycle_notes_match_is_not_over_broad():
    # A lifecycle row that merely mentions "interrupted" in a diagnostic context
    # (not as the terminal cause) is NOT bucketed.
    r = _row(gate="lifecycle", subject="cleanup",
             notes="worker 3 reported an interrupted download, retried ok")
    assert report.nonactionable_failure_reason(r) is None
    # But the real operator-interrupt phrasings ARE bucketed.
    assert report.nonactionable_failure_reason(
        _row(gate="lifecycle", subject="cleanup", notes="run interrupted")) is not None


def test_real_product_and_infra_failures_are_actionable():
    # A GUI scenario fail, a bats fail, a vm_provision fail, and an API-outage
    # row are all actionable — none may be silently bucketed as "expected".
    for r in (
        _row(gate="gui", subject="permissions-gui/21-tier5-close-cleanup.md",
             notes="agent status=FAIL rc=0"),
        _row(gate="bats", subject="silo-egress.bats", notes="raw_rc=1"),
        _row(gate="gui", subject="x", notes="agent command rc=1 status=UNKNOWN"),
        _row(gate="gui-admin", subject="golden-build", exit_class="vm_provision",
             notes="run-golden build failed"),
    ):
        assert report.nonactionable_failure_reason(r) is None, r["subject"]


def test_partition_normalizes_status_like_the_classifier():
    # A padded / upper-cased status must not be silently dropped from BOTH
    # buckets — partition_failures normalizes the same way the classifier does.
    rows = [
        _row(status="FAIL", gate="gui", subject="upper-fail"),
        _row(status=" fail ", gate="gui", subject="padded-fail"),
        _row(status="Blocked", gate="release-manifest", subject="unpopulated"),
    ]
    actionable, expected = report.partition_failures(rows)
    assert [r["subject"] for r in actionable] == ["upper-fail", "padded-fail"]
    assert [r["subject"] for r, _ in expected] == ["unpopulated"]


def test_pass_and_skip_rows_are_never_nonactionable_failures():
    # The classifier only speaks to FAIL/BLOCKED; a pass/skip returns None.
    assert report.nonactionable_failure_reason(_row(status="pass")) is None
    assert report.nonactionable_failure_reason(
        _row(status="skip", gate="release-manifest")) is None


def test_partition_failures_splits_and_preserves_order():
    rows = [
        {"status": "pass", "gate": "host", "subject": "ok"},
        _row(gate="gui", subject="real-fail-1"),
        _row(gate="release-manifest", subject="unpopulated", status="blocked"),
        _row(gate="gui", subject="real-fail-2"),
        _row(gate="lifecycle", subject="INT", notes="run interrupted"),
        {"status": "skip", "gate": "host", "subject": "skipme"},
    ]
    actionable, expected = report.partition_failures(rows)
    assert [r["subject"] for r in actionable] == ["real-fail-1", "real-fail-2"]
    assert [r["subject"] for r, _ in expected] == ["unpopulated", "INT"]
    assert all(isinstance(reason, str) and reason for _, reason in expected)
