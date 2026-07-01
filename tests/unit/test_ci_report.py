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


# --- correlated_infra_bursts (report-only outage detector) ---
# A contiguous run of same-classifier infra failures in one lane is an OUTAGE,
# not N product bugs. False POSITIVES (calling product bugs an outage) are the
# dangerous direction, so the detector is conservative.


def _att(cls, *, lane="admin", status="UNKNOWN", rc="1", end=None, subject="s"):
    a = {"lane": lane, "classifier": cls, "status": status, "agent_rc": rc,
         "subject": subject}
    if end is not None:
        a["end_epoch"] = str(end)
    return a


def _clean(*, lane="admin", end=None, subject="ok"):
    return _att("", lane=lane, status="PASS", rc="0", end=end, subject=subject)


def test_burst_detects_api_outage_run_within_window():
    # 35 agent-api-unreachable in a row within ~20 min => one burst.
    atts = [_att("agent-api-unreachable", end=1000 + i * 30, subject=f"s{i}")
            for i in range(35)]
    bursts = report.correlated_infra_bursts(atts)
    assert len(bursts) == 1
    b = bursts[0]
    assert b["classifier"] == "agent-api-unreachable"
    assert b["count"] == 35
    assert b["trigger"] == "consecutive"
    assert b["lane"] == "admin"


def test_burst_consecutive_run_spread_past_window_is_not_a_burst():
    # 6 infra fails spaced 10 min apart (>20 min total), each followed by passes,
    # so no 5-in-a-row consecutive run AND infra is a minority of the last 10 =>
    # neither trigger fires.
    atts = []
    t = 1000
    for _ in range(6):
        atts.append(_att("transport-timeout", end=t)); t += 600
        atts.append(_clean(end=t)); t += 60
        atts.append(_clean(end=t)); t += 60
    assert report.correlated_infra_bursts(atts) == []


def test_burst_product_failures_never_fire():
    # 10 genuine product-fails (non-infra classifier) => no burst.
    atts = [_att("product-fail", end=1000 + i * 20) for i in range(10)]
    assert report.correlated_infra_bursts(atts) == []


def test_burst_agent_tooling_is_excluded():
    atts = [_att("agent-tooling", end=1000 + i * 20) for i in range(8)]
    assert report.correlated_infra_bursts(atts) == []


def test_burst_fraction_trigger_when_interleaved_with_passes():
    # Parallel-worker interleave: passes break consecutiveness, but >=50% of the
    # last 10 completions are the same infra classifier => fraction trigger.
    atts = []
    for i in range(10):
        atts.append(_att("agent-api-unreachable", end=1000 + i * 40, subject=f"f{i}")
                    if i % 2 == 0 else _clean(end=1000 + i * 40, subject=f"p{i}"))
    bursts = report.correlated_infra_bursts(atts)
    assert len(bursts) == 1
    assert bursts[0]["trigger"] == "fraction"
    assert bursts[0]["classifier"] == "agent-api-unreachable"


def test_burst_is_per_lane_not_diluted_across_lanes():
    # A qdwin-lane outage must fire even though the admin lane is all green.
    atts = [_clean(lane="admin", end=1000 + i * 30) for i in range(20)]
    atts += [_att("agent-api-unreachable", lane="qdwin", end=1000 + i * 30)
             for i in range(6)]
    bursts = report.correlated_infra_bursts(atts)
    assert len(bursts) == 1
    assert bursts[0]["lane"] == "qdwin"


def test_burst_two_infra_classifiers_below_threshold_do_not_fire():
    # 3 transport-timeout then 3 vm-provision: neither reaches min_run=5
    # consecutively, and no single classifier is >=50% of the (6) last attempts.
    atts = [_att("transport-timeout", end=1000 + i * 20) for i in range(3)]
    atts += [_att("vm-provision", status="FAIL", end=1100 + i * 20) for i in range(3)]
    assert report.correlated_infra_bursts(atts) == []


def test_burst_without_epochs_still_surfaces_run_window_none():
    # Old-schema rows (no end_epoch) keep file order; the run is still surfaced
    # for a human, with window_s = None (time gate skipped).
    atts = [_att("agent-api-unreachable") for _ in range(6)]
    bursts = report.correlated_infra_bursts(atts)
    assert len(bursts) == 1
    assert bursts[0]["window_s"] is None


def test_burst_finds_dense_subrun_inside_a_longer_run():
    # 6 same-classifier infra fails at 0,5,10,15,20,25 min: the whole run spans
    # 25 min (>window), but the first 5 are a valid 5-in-20-min burst. The
    # sliding window must surface that subrun (count 5), not reject the run.
    atts = [_att("agent-api-unreachable", end=1000 + i * 300) for i in range(6)]
    bursts = report.correlated_infra_bursts(atts)
    assert len(bursts) == 1
    assert bursts[0]["trigger"] == "consecutive"
    assert bursts[0]["count"] == 5
    assert bursts[0]["window_s"] <= 1200


def test_burst_fraction_does_not_fire_on_a_tied_top_classifier():
    # 5 transport-timeout + 5 vm-provision in the last 10: each is >=50% but the
    # top is not unique, so the (single-classifier) fraction trigger must not fire.
    # Interleave so neither forms a 5-in-a-row consecutive run either.
    atts = []
    for i in range(10):
        cls = "transport-timeout" if i % 2 == 0 else "vm-provision"
        atts.append(_att(cls, status="FAIL", end=1000 + i * 30))
    assert report.correlated_infra_bursts(atts) == []


def test_burst_empty_and_healthy_runs_return_nothing():
    assert report.correlated_infra_bursts([]) == []
    assert report.correlated_infra_bursts(
        [_clean(end=1000 + i * 10) for i in range(20)]) == []
