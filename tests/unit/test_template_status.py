"""Unit tests for qdistro-template-status (fableplan2 task 04).

Status is read-only and assembled from the on-disk model: bindings, the
per-boot runtime status files, candidate manifests + validation reports, and
pin receipts. No daemon, no podman."""
from __future__ import annotations

import os

import qdistro_templates as qt
import qdistro_template_status as status


GEN_A = "sha256:" + "a" * 64
GEN_B = "sha256:" + "b" * 64


def _gen_record(layout, template, gen):
    gen_dir = layout.generation_dir(template, gen)
    os.makedirs(gen_dir, exist_ok=True)
    qt.write_toml_atomic(os.path.join(gen_dir, "manifest.toml"), {
        "template": template, "run_id": "r", "image_digest": gen,
        "image_id": gen, "containerfile_digest": gen,
        "build_command": "x", "network_mode": "unrestricted",
        "artifact_manifest": [], "generation_ref": gen,
    }, 0o644)


def _binding(layout, silo, active, prev=None):
    qt.write_binding(layout.binding_file(silo), {
        "silo": silo, "template": "tier2-browser", "backend": "podman-image",
        "active_generation": active, "previous_generations": prev or [],
        "state_path": f"/var/lib/qdistro/silos/{silo}/state",
        "activation_policy": "manual", "identity_revision": 1,
    })
    _gen_record(layout, "tier2-browser", active)
    for g in (prev or []):
        _gen_record(layout, "tier2-browser", g)


def _run_status(run_dir, silo, gen):
    os.makedirs(run_dir, exist_ok=True)
    qt.atomic_write(os.path.join(run_dir, silo), f"generation = {gen!r}\n", 0o644)


def test_restart_pending_when_bound_differs_from_running(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    run_dir = str(tmp_path / "run")
    _binding(layout, "browser1", GEN_B, prev=[GEN_A])
    _run_status(run_dir, "browser1", GEN_A)  # still running the old gen
    st = status.collect(layout=layout, run_status_dir=run_dir)
    s = st["silos"][0]
    assert s["silo"] == "browser1"
    assert s["bound_generation"] == GEN_B
    assert s["running_generation"] == GEN_A
    assert s["restart_pending"] is True
    assert [t["generation"] for t in s["rollback_targets"]] == [GEN_A]


def test_no_restart_pending_when_in_sync(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    run_dir = str(tmp_path / "run")
    _binding(layout, "browser1", GEN_B, prev=[GEN_A])
    _run_status(run_dir, "browser1", GEN_B)
    s = status.collect(layout=layout, run_status_dir=run_dir)["silos"][0]
    assert s["restart_pending"] is False


def test_running_unknown_when_no_status_file(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "browser1", GEN_A)
    s = status.collect(layout=layout, run_status_dir=str(tmp_path / "run"))["silos"][0]
    assert s["running_generation"] is None
    assert s["restart_pending"] is False  # never started ≠ pending


def test_rollback_target_pin_expiry_reported(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "browser1", GEN_B, prev=[GEN_A])
    qt.write_pin(os.path.join(layout.pins_for("tier2-browser", GEN_A),
                              "rollback-window.toml"), {
        "owner_type": "silo", "owner_id": "browser1",
        "reason": "rollback-window", "generation": GEN_A,
        "template": "tier2-browser", "expires_at": "2026-07-01T00:00:00Z",
    })
    s = status.collect(layout=layout, run_status_dir=str(tmp_path / "run"))["silos"][0]
    t = s["rollback_targets"][0]
    assert t["generation"] == GEN_A
    assert t["pin_expires_at"] == "2026-07-01T00:00:00Z"
    assert t["image_present"] is True


def test_parked_validated_candidate_reported(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "browser1", GEN_A)
    cdir = layout.candidate_dir("tier2-browser", "20260610T120000Z-abcd0001")
    os.makedirs(os.path.join(cdir, "evidence"))
    qt.write_toml_atomic(os.path.join(cdir, "evidence", "validation.toml"),
                         {"result": "validated"}, 0o644)
    qt.set_candidate_state(cdir, "validated")
    # a non-validated candidate must NOT appear
    cdir2 = layout.candidate_dir("tier2-browser", "20260610T120001Z-abcd0002")
    os.makedirs(cdir2)
    qt.set_candidate_state(cdir2, "built")
    s = status.collect(layout=layout, run_status_dir=str(tmp_path / "run"))["silos"][0]
    parked = s["parked_candidates"]
    assert len(parked) == 1
    assert parked[0]["run_id"] == "20260610T120000Z-abcd0001"
    assert parked[0]["validation_result"] == "validated"


def test_unreadable_binding_surfaces_as_error_row(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    # A tag (non-digest) binding is invalid — status must surface it, not drop.
    qt.write_toml_atomic(layout.binding_file("broken"),
                         {"silo": "broken", "template": "t",
                          "backend": "podman-image",
                          "active_generation": "latest",
                          "previous_generations": [],
                          "state_path": "/x", "activation_policy": "manual",
                          "identity_revision": 1}, 0o600)
    s = status.collect(layout=layout, run_status_dir=str(tmp_path / "run"))["silos"][0]
    assert s["silo"] == "broken" and "error" in s


def test_main_keyvalue_and_json(tmp_path, monkeypatch, capsys):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "browser1", GEN_B, prev=[GEN_A])
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("QDISTRO_RUN_STATUS_DIR", str(tmp_path / "run"))
    _run_status(str(tmp_path / "run"), "browser1", GEN_A)
    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "restart_pending=yes" in out
    # Strictly KEY=VALUE: every whitespace-separated token parses as k=v with
    # a single-token value (no bare words, no spaces inside a value).
    for line in out.splitlines():
        if not line.strip():
            continue
        for tok in line.split():
            assert "=" in tok, f"non KEY=VALUE token {tok!r} in {line!r}"
        assert line.split()[0].startswith("record="), line
    assert status.main(["--json"]) == 0
    import json as _json
    data = _json.loads(capsys.readouterr().out)
    assert data["silos"][0]["restart_pending"] is True
