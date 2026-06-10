"""Unit tests for qdistro-template-freshness (todo/fableplan task 08):
the condition evaluation, the staleness math, and a satisfied-conditions
run that produces a validated candidate without touching bindings."""
from __future__ import annotations

import os
import time

import qdistro_templates as qt
import qdistro_template_freshness as fresh


def _layout(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    return layout


def _derived_policy(layout, template="tier2-dev", cls="derived"):
    qt.write_toml_atomic(layout.template_policy(template), {
        "template": {"class": cls,
                     "state_boundary": {"class": "recipe-derived-toolchain",
                                        "enforced": "true"},
                     "build": {"containerfile": "Containerfile.tier2-dev"},
                     "probe": [{"name": "p", "kind": "process", "command": "true"}]}
    }, 0o644)


# --------------------------------------------------------------------------
# staleness math
# --------------------------------------------------------------------------

def test_staleness_label():
    assert fresh.staleness_label(None) == "never"
    assert fresh.staleness_label(0) == "ok"
    assert fresh.staleness_label(6 * 86400) == "ok"
    assert fresh.staleness_label(8 * 86400) == "warn"
    assert fresh.staleness_label(31 * 86400) == "needs-attention"


def test_last_success_age_and_is_stale():
    now = 1_700_000_000.0
    assert fresh.last_success_age({}, now) is None
    assert fresh.is_stale({}, now, 7 * 86400) is True
    status = {"last_success_epoch": now - 3 * 86400}
    assert abs(fresh.last_success_age(status, now) - 3 * 86400) < 1
    assert fresh.is_stale(status, now, 7 * 86400) is False
    old = {"last_success_epoch": now - 10 * 86400}
    assert fresh.is_stale(old, now, 7 * 86400) is True


def test_night_window():
    def at(hour):
        # build an epoch at the given local hour
        lt = list(time.localtime(1_700_000_000))
        lt[3] = hour
        return time.mktime(time.struct_time(lt))
    assert fresh.in_night_window(at(23)) is True
    assert fresh.in_night_window(at(2)) is True
    assert fresh.in_night_window(at(12)) is False
    assert fresh.in_night_window(at(21)) is False


# --------------------------------------------------------------------------
# condition evaluation
# --------------------------------------------------------------------------

class _Proc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_check_network_metered_blocks(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "CONNECTIVITY" in cmd:
            return _Proc("full\n", 0)
        return _Proc("yes (1)\n", 0)  # METERED
    monkeypatch.setattr(fresh.subprocess, "run", fake_run)
    ok, detail = fresh.check_network()
    assert ok is False and "metered" in detail


def test_check_network_full_nonmetered_ok(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "CONNECTIVITY" in cmd:
            return _Proc("full\n", 0)
        return _Proc("no (4)\n", 0)
    monkeypatch.setattr(fresh.subprocess, "run", fake_run)
    ok, _ = fresh.check_network()
    assert ok is True


def test_check_network_not_full_blocks(monkeypatch):
    monkeypatch.setattr(fresh.subprocess, "run",
                        lambda cmd, *a, **k: _Proc("limited\n", 0))
    ok, detail = fresh.check_network()
    assert ok is False and "limited" in detail


def test_check_network_metered_field_unsupported_permits(monkeypatch):
    # If the METERED field query fails (nmcli build differences), we permit
    # rather than block, but never claim "non-metered".
    def fake_run(cmd, *a, **k):
        if "CONNECTIVITY" in cmd:
            return _Proc("full\n", 0)
        return _Proc("error\n", 2)  # METERED query failed
    monkeypatch.setattr(fresh.subprocess, "run", fake_run)
    ok, detail = fresh.check_network()
    assert ok is True and "unknown" in detail


def test_check_network_nm_unavailable_permits(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("nmcli")
    monkeypatch.setattr(fresh.subprocess, "run", boom)
    ok, _ = fresh.check_network()
    assert ok is True


def test_check_idle_active_session_blocks(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "list-sessions" in cmd:
            return _Proc("3 1000 admin seat0 tty2\n", 0)
        return _Proc("Type=wayland\nIdleHint=no\n", 0)
    monkeypatch.setattr(fresh.subprocess, "run", fake_run)
    ok, detail = fresh.check_idle()
    assert ok is False and "active" in detail


def test_check_idle_idle_session_permits(monkeypatch):
    def fake_run(cmd, *a, **k):
        if "list-sessions" in cmd:
            return _Proc("3 1000 admin seat0 tty2\n", 0)
        return _Proc("Type=wayland\nIdleHint=yes\n", 0)
    monkeypatch.setattr(fresh.subprocess, "run", fake_run)
    ok, _ = fresh.check_idle()
    assert ok is True


def test_evaluate_conditions_force_overrides(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    monkeypatch.setattr(fresh, "check_free_space", lambda lay: (False, "low disk"))
    ok, results = fresh.evaluate_conditions(layout, 1_700_000_000.0, force=False)
    assert ok is False
    assert any(r["name"] == "free_space" and not r["ok"] for r in results)
    ok2, _ = fresh.evaluate_conditions(layout, 1_700_000_000.0, force=True)
    assert ok2 is True


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def test_run_skips_when_conditions_not_met(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _derived_policy(layout)
    monkeypatch.setattr(fresh, "check_free_space", lambda lay: (False, "low disk"))
    called = []
    summary = fresh.run_freshness(layout=layout, now=1_700_000_000.0,
                                  builder=lambda t: (called.append(t), (0, "x"))[1],
                                  validator=lambda r: 0)
    assert summary["conditions_ok"] is False
    assert summary.get("skipped") is True
    assert called == [], "no build when conditions are not met"


def test_run_builds_validates_without_touching_bindings(tmp_path):
    layout = _layout(tmp_path)
    _derived_policy(layout)
    now = 1_700_000_000.0
    built = []

    def builder(template):
        built.append(template)
        return 0, "fresh-1"  # (rc, run_id)

    validated = []

    def validator(run_id):
        validated.append(run_id)
        return 0

    summary = fresh.run_freshness(layout=layout, now=now, force=True,
                                  builder=builder, validator=validator)
    assert built == ["tier2-dev"]
    assert validated == ["fresh-1"]
    actions = [t["action"] for t in summary["templates"]]
    assert "validated" in actions
    # status records last_success; NO binding was created
    status = fresh.read_status(layout, "tier2-dev")
    assert status["last_result"] == "validated"
    assert status["staleness"] == "ok"
    assert not os.path.isfile(layout.binding_file("tier2-dev"))
    assert os.listdir(layout.bindings_dir) == []


def test_run_skips_fresh_template(tmp_path):
    layout = _layout(tmp_path)
    _derived_policy(layout)
    now = 1_700_000_000.0
    # already-fresh status
    fresh._write_status(layout, "tier2-dev", {"last_success_epoch": now - 86400})
    built = []
    fresh.run_freshness(layout=layout, now=now, force=True,
                        builder=lambda t: (built.append(t), (0, "x"))[1],
                        validator=lambda r: 0)
    assert built == [], "a fresh template is not rebuilt"


def test_run_build_failure_is_degraded(tmp_path):
    layout = _layout(tmp_path)
    _derived_policy(layout)
    now = 1_700_000_000.0
    summary = fresh.run_freshness(layout=layout, now=now, force=True,
                                  builder=lambda t: (1, None),  # build fails
                                  validator=lambda r: 0)
    assert any(t["action"] == "build-failed" for t in summary["templates"])
    status = fresh.read_status(layout, "tier2-dev")
    assert status["last_result"] == "degraded"
    assert status["staleness"] == "never"  # never had a good build
    assert not os.path.isfile(layout.binding_file("tier2-dev"))


def test_run_validates_exact_built_candidate_not_newest_mtime(tmp_path):
    # The builder returns the run-id it created; freshness must validate THAT
    # one, even if a stale pre-existing candidate has a newer mtime.
    layout = _layout(tmp_path)
    _derived_policy(layout)
    now = 1_700_000_000.0
    # a stale candidate with a deliberately newer mtime
    stale = layout.candidate_dir("tier2-dev", "stale-newer")
    os.makedirs(stale)
    os.utime(stale, (now + 1000, now + 1000))
    validated = []
    fresh.run_freshness(
        layout=layout, now=now, force=True,
        builder=lambda t: (0, "the-real-one"),
        validator=lambda run_id: (validated.append(run_id), 0)[1])
    assert validated == ["the-real-one"], "must validate the candidate just built"


def test_run_ignores_artifact_templates(tmp_path):
    layout = _layout(tmp_path)
    _derived_policy(layout, template="wine-office", cls="artifact")
    built = []
    fresh.run_freshness(layout=layout, now=1_700_000_000.0, force=True,
                        builder=lambda t: (built.append(t), (0, "x"))[1],
                        validator=lambda r: 0)
    assert built == [], "freshness rebuilds derived templates only"
