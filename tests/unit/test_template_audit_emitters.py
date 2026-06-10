"""Assert each template lifecycle emitter writes its audit event
(todo/fableplan task 06). podman is stubbed; the launch path and promote
run against tmp state."""
from __future__ import annotations

import os

import qdistro_templates as qt
import qdistro_template_audit as audit
import qdistro_template_build as build
import qdistro_template_validate as validate
import qdistro_template_promote as promote
import qdistro_resolve_binding as rb


GEN = "sha256:" + "a" * 64


def _audit_events(layout):
    db = os.path.join(layout.var, "audit", "template_audit.sqlite")
    log = audit.TemplateAuditLog(db)
    try:
        return [r["event"] for r in log.recent()]
    finally:
        log.close()


def _layout(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    return layout


def _policy(layout):
    policy = {"template": {"class": "derived",
                           "state_boundary": {"class": "recipe-derived-toolchain",
                                              "enforced": "true"},
                           "build": {"containerfile": "Containerfile.tier2-dev"},
                           "probe": [{"name": "p", "kind": "process", "command": "true"}]}}
    qt.write_toml_atomic(layout.template_policy("tier2-dev"), policy, 0o644)


def test_build_emits_started_and_finished(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _policy(layout)
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))
    monkeypatch.setattr(build.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 0})())
    monkeypatch.setattr(build, "_podman_inspect",
                        lambda tag, fmt: "sha256:" + ("d" * 64) if "Digest" in fmt else "e" * 64)
    assert build.build("tier2-dev", layout=layout) == 0
    events = _audit_events(layout)
    assert "template.build.started" in events
    assert "template.build.finished" in events


def test_validate_emits_finished(tmp_path):
    layout = _layout(tmp_path)
    _policy(layout)
    run_id = "20260610T120000Z-deadbeef"
    cdir = layout.candidate_dir("tier2-dev", run_id)
    os.makedirs(os.path.join(cdir, "evidence"))
    manifest = {"template": "tier2-dev", "run_id": run_id, "image_digest": GEN,
                "image_id": GEN, "containerfile_digest": GEN,
                "build_command": "x", "network_mode": "unrestricted",
                "artifact_manifest": [], "generation_ref": GEN}
    qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"), manifest, 0o644)
    qt.set_candidate_state(cdir, "built")

    def runner(image_ref, probe, ctr, evidence_dir=None):
        return {"name": probe["name"], "kind": probe["kind"], "class": "local-runtime",
                "required": True, "result": "pass", "duration_seconds": 0.1,
                "reason": "", "artifacts": []}

    assert validate.validate(run_id, layout=layout, runner=runner) == 0
    assert "template.validate.finished" in _audit_events(layout)


def _validated_candidate(layout, run_id, gen):
    cdir = layout.candidate_dir("tier2-dev", run_id)
    os.makedirs(os.path.join(cdir, "evidence"))
    manifest = {"template": "tier2-dev", "run_id": run_id, "image_digest": gen,
                "image_id": gen, "containerfile_digest": gen, "build_command": "x",
                "network_mode": "unrestricted", "artifact_manifest": [],
                "generation_ref": gen}
    qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"), manifest, 0o644)
    report = {"run_id": run_id, "template": "tier2-dev", "generation_ref": gen,
              "result": "validated", "checks_total": 1, "checks_failed": 0,
              "check": [{"name": "p", "kind": "process", "class": "local-runtime",
                         "required": True, "result": "pass", "duration_seconds": 0.1,
                         "reason": ""}]}
    qt.write_toml_atomic(os.path.join(cdir, "evidence", "validation.toml"), report, 0o644)
    qt.set_candidate_state(cdir, "validated")


def test_promote_emits_requested_and_applied(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "run-a", GEN)
    assert promote.promote("dev-silo", "run-a", layout=layout,
                           resolver=lambda *a: (_ for _ in ()).throw(AssertionError)) == 0
    events = _audit_events(layout)
    assert "template.promote.requested" in events
    assert "template.promote.applied" in events


def test_promote_emits_refused(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "run-a", GEN)
    os.unlink(layout.candidate_dir("tier2-dev", "run-a") + "/evidence/validation.toml")
    assert promote.promote("dev-silo", "run-a", layout=layout) == 1
    assert "template.promote.refused" in _audit_events(layout)


def _audit_rows(layout):
    db = os.path.join(layout.var, "audit", "template_audit.sqlite")
    log = audit.TemplateAuditLog(db)
    try:
        return log.recent()
    finally:
        log.close()


def test_build_preflight_failure_emits_finished(tmp_path):
    # Missing policy: a build attempt that fails before a candidate exists
    # still records a failed build.finished.
    layout = _layout(tmp_path)
    assert build.build("ghost", layout=layout) == 2
    rows = _audit_rows(layout)
    fin = [r for r in rows if r["event"] == "template.build.finished"]
    assert fin and fin[0]["result"] == "failed"


def test_validate_refusal_emits_finished(tmp_path):
    layout = _layout(tmp_path)
    assert validate.validate("20260101T000000Z-00000000", layout=layout) == 2
    rows = _audit_rows(layout)
    fin = [r for r in rows if r["event"] == "template.validate.finished"]
    assert fin and fin[0]["result"] == "refused"


def test_promote_applied_carries_old_new_generation(tmp_path):
    layout = _layout(tmp_path)
    GEN_B = "sha256:" + "b" * 64
    _validated_candidate(layout, "run-a", GEN)
    promote.promote("dev-silo", "run-a", layout=layout,
                    resolver=lambda *a: (_ for _ in ()).throw(AssertionError))
    _validated_candidate(layout, "run-b", GEN_B)
    promote.promote("dev-silo", "run-b", layout=layout,
                    resolver=lambda *a: (_ for _ in ()).throw(AssertionError))
    rows = _audit_rows(layout)
    applied = [r for r in rows if r["event"] == "template.promote.applied"
               and r["new_generation"] == GEN_B][0]
    assert applied["old_generation"] == GEN
    assert applied["identity_revision"] == 2


def test_binding_activated_emitted_on_change(tmp_path):
    layout = _layout(tmp_path)
    # promote to create a binding + generation record
    _validated_candidate(layout, "run-a", GEN)
    promote.promote("dev-silo", "run-a", layout=layout,
                    resolver=lambda *a: (_ for _ in ()).throw(AssertionError))
    run_dir = str(tmp_path / "run")
    import qdistro_resolve_binding as rbmod
    rbmod.RUN_STATUS_DIR = run_dir  # avoid writing /run
    rc, gen = rb.resolve("dev-silo", layout=layout, record=True)
    assert rc == 0 and gen == GEN
    assert "template.binding.activated" in _audit_events(layout)
