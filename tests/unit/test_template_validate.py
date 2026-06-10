"""Unit tests for qdistro-template-validate (todo/fableplan task 03).

The probe/report/state logic is tested with the container runner stubbed;
a real disposable-runtime validation (pass + a deliberately broken
candidate) is exercised by the rootless-podman smoke + the VM bats suite."""
from __future__ import annotations

import os

import pytest

import qdistro_templates as qt
import qdistro_template_validate as validate


def _built_candidate(tmp_path, *, probes=None, state="built"):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    if probes is None:
        probes = [
            {"name": "process-starts", "kind": "process", "command": "true"},
            {"name": "gcc", "kind": "command", "command": "gcc --version"},
            {"name": "hello", "kind": "compile-run"},
        ]
    policy = {
        "template": {
            "class": "derived",
            "state_boundary": {"class": "recipe-derived-toolchain", "enforced": "true"},
            "build": {"containerfile": "Containerfile.tier2-dev"},
            "probe": probes,
        }
    }
    qt.write_toml_atomic(layout.template_policy("tier2-dev"), policy, 0o644)
    run_id = "20260610T120000Z-deadbeef"
    cdir = layout.candidate_dir("tier2-dev", run_id)
    os.makedirs(cdir)
    manifest = {
        "template": "tier2-dev", "run_id": run_id,
        "image_digest": "sha256:" + "a" * 64, "image_id": "sha256:" + "b" * 64,
        "containerfile_digest": "sha256:" + "c" * 64,
        "build_command": "podman build ...", "network_mode": "unrestricted",
        "artifact_manifest": [], "generation_ref": "sha256:" + "b" * 64,
    }
    qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"), manifest, 0o644)
    qt.set_candidate_state(cdir, state)
    return layout, run_id, cdir


def _pass_runner(image_ref, probe, ctr):
    return {"name": probe["name"], "kind": probe["kind"], "class": "local-runtime",
            "required": bool(probe.get("required", True)), "result": "pass",
            "duration_seconds": 0.1, "reason": ""}


def _fail_runner_for(target):
    def runner(image_ref, probe, ctr):
        r = _pass_runner(image_ref, probe, ctr)
        if probe["name"] == target:
            r["result"] = "fail"
            r["reason"] = "boom"
        return r
    return runner


def test_find_candidate(tmp_path):
    layout, run_id, cdir = _built_candidate(tmp_path)
    assert validate.find_candidate(layout, run_id) == ("tier2-dev", cdir)
    assert validate.find_candidate(layout, "20260101T000000Z-00000000") is None


def test_find_candidate_rejects_unsafe_run_id(tmp_path):
    layout = qt.Layout(var=str(tmp_path / "var"))
    with pytest.raises(qt.TemplateError):
        validate.find_candidate(layout, "../../etc")


def test_probe_argv_kinds():
    assert validate._probe_argv({"kind": "process"})[:2] == ["/bin/sh", "-c"]
    assert validate._probe_argv({"kind": "command", "command": "gcc --version"})[2] == "gcc --version"
    assert validate._HELLO_SENTINEL in validate._probe_argv({"kind": "compile-run"})[2]
    with pytest.raises(qt.TemplateError):
        validate._probe_argv({"kind": "window", "name": "win"})
    with pytest.raises(qt.TemplateError):
        validate._probe_argv({"kind": "bogus", "name": "x"})
    with pytest.raises(qt.TemplateError):
        validate._probe_argv({"kind": "command", "name": "x"})  # no command


def test_validate_all_pass(tmp_path):
    layout, run_id, cdir = _built_candidate(tmp_path)
    rc = validate.validate(run_id, layout=layout, runner=_pass_runner)
    assert rc == 0
    assert qt.candidate_state(cdir) == "validated"
    report = qt.read_toml(os.path.join(cdir, "evidence", "validation.toml"))
    assert report["result"] == "validated"
    assert report["checks_total"] == 3 and report["checks_failed"] == 0
    assert len(report["check"]) == 3
    manifest = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    assert manifest["validation"]["result"] == "validated"
    assert manifest["validation"]["report"] == "evidence/validation.toml"


def test_validate_required_failure_sets_failed(tmp_path):
    layout, run_id, cdir = _built_candidate(tmp_path)
    rc = validate.validate(run_id, layout=layout, runner=_fail_runner_for("gcc"))
    assert rc == 1
    assert qt.candidate_state(cdir) == "failed"
    report = qt.read_toml(os.path.join(cdir, "evidence", "validation.toml"))
    assert report["result"] == "failed"
    assert report["checks_failed"] == 1
    failed = [c for c in report["check"] if c["result"] == "fail"]
    assert failed[0]["name"] == "gcc" and failed[0]["reason"] == "boom"


def test_validate_non_required_failure_still_validated(tmp_path):
    probes = [
        {"name": "process-starts", "kind": "process", "command": "true"},
        {"name": "optional", "kind": "command", "command": "flaky", "required": False},
    ]
    layout, run_id, cdir = _built_candidate(tmp_path, probes=probes)
    rc = validate.validate(run_id, layout=layout, runner=_fail_runner_for("optional"))
    assert rc == 0
    assert qt.candidate_state(cdir) == "validated"


def test_validate_refuses_non_built(tmp_path):
    layout, run_id, cdir = _built_candidate(tmp_path, state="failed")
    assert validate.validate(run_id, layout=layout, runner=_pass_runner) == 2
    # state untouched
    assert qt.candidate_state(cdir) == "failed"


def test_validate_missing_candidate(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    assert validate.validate("20260101T000000Z-00000000", layout=layout) == 2


def test_validate_unsupported_probe_fails_with_evidence(tmp_path):
    # A 'window' probe is deferred; it must become a FAILED check with a
    # report and state=failed, not abort the run with no evidence.
    probes = [
        {"name": "process-starts", "kind": "process", "command": "true"},
        {"name": "shows-window", "kind": "window"},
    ]
    layout, run_id, cdir = _built_candidate(tmp_path, probes=probes)
    # Use the real run_probe (which calls _probe_argv -> raises) but never
    # actually launches podman because the window kind raises first.
    rc = validate.validate(run_id, layout=layout)
    assert rc == 1
    assert qt.candidate_state(cdir) == "failed"
    report = qt.read_toml(os.path.join(cdir, "evidence", "validation.toml"))
    win = [c for c in report["check"] if c["name"] == "shows-window"][0]
    assert win["result"] == "fail"
    assert "window" in win["reason"] or "setup error" in win["reason"]


def test_validate_manifest_identity_mismatch_refused(tmp_path):
    layout, run_id, cdir = _built_candidate(tmp_path)
    # Corrupt the manifest's run_id so it disagrees with the dir.
    mpath = os.path.join(cdir, "manifest.toml")
    manifest = qt.read_manifest(mpath)
    manifest["run_id"] = "20260101T000000Z-00000000"
    qt.write_toml_atomic(mpath, manifest, 0o644)
    assert validate.validate(run_id, layout=layout, runner=_pass_runner) == 2
    # state untouched (still built), no report written
    assert qt.candidate_state(cdir) == "built"
    assert not os.path.exists(os.path.join(cdir, "evidence", "validation.toml"))


def test_run_probe_compile_sentinel_logic(monkeypatch):
    # gcc "passes" (exit 0) but the program never prints the sentinel -> fail.
    class Proc:
        returncode = 0
        stdout = "wrong-output\n"
        stderr = ""

    monkeypatch.setattr(validate.subprocess, "run", lambda *a, **k: Proc())
    res = validate.run_probe("sha256:" + "b" * 64,
                             {"name": "hello", "kind": "compile-run"}, "ctr")
    assert res["result"] == "fail"
    assert "sentinel" in res["reason"]


def test_run_probe_network_none_for_local(monkeypatch):
    captured = {}

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(validate.subprocess, "run", fake_run)
    validate.run_probe("sha256:" + "b" * 64,
                       {"name": "p", "kind": "command", "command": "true"}, "ctr")
    assert "--network=none" in captured["cmd"]
    # remote-read probe gets network
    captured.clear()
    validate.run_probe("sha256:" + "b" * 64,
                       {"name": "p", "kind": "command", "command": "true",
                        "class": "remote-read"}, "ctr")
    assert "--network=none" not in captured["cmd"]
