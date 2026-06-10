"""Unit tests for qdistro-resolve-binding (todo/fableplan task 05).

The launch path resolves a silo's active_generation digest from its
binding, with no tag fallback; a non-digest binding is a hard error and an
absent binding means the silo is untemplated."""
from __future__ import annotations

import os

import qdistro_templates as qt
import qdistro_resolve_binding as rb

import pytest


GEN = "sha256:" + "a" * 64


def _generation(layout, template, gen):
    """Materialize a minimal promoted generation record so resolve() — which
    now requires one — succeeds."""
    gen_dir = layout.generation_dir(template, gen)
    os.makedirs(gen_dir, exist_ok=True)
    manifest = {
        "template": template, "run_id": "r1",
        "image_digest": gen, "image_id": gen, "containerfile_digest": gen,
        "build_command": "podman build ...", "network_mode": "unrestricted",
        "artifact_manifest": [], "generation_ref": gen,
    }
    qt.write_toml_atomic(os.path.join(gen_dir, "manifest.toml"), manifest, 0o644)


def _binding(layout, silo, active=GEN, prev=None, with_generation=True):
    binding = {
        "silo": silo, "template": "tier2-dev", "backend": "podman-image",
        "active_generation": active, "previous_generations": prev or [],
        "state_path": f"/var/lib/qdistro/silos/{silo}/state",
        "activation_policy": "manual", "identity_revision": 1,
    }
    # write raw (bypass validation) so we can also author a tag for the
    # hard-error test.
    qt.write_toml_atomic(layout.binding_file(silo), binding, 0o600)
    if with_generation and qt.is_digest(active):
        _generation(layout, "tier2-dev", active)


def test_resolve_untemplated_returns_3(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    rc, gen = rb.resolve("dev-silo", layout=layout)
    assert rc == 3 and gen is None


def test_resolve_digest_binding(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    rc, gen = rb.resolve("dev-silo", layout=layout)
    assert rc == 0 and gen == GEN


def test_resolve_tag_binding_is_hard_error(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo", active="qdistro/tier2-dev:latest")
    with pytest.raises(qt.TemplateError):
        rb.resolve("dev-silo", layout=layout)


def test_resolve_digest_without_generation_record_is_hard_error(tmp_path):
    # A binding whose active_generation is a valid digest but has NO promoted
    # generation record (e.g. a parked candidate digest) must be refused —
    # the launch path only launches promoted generations.
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo", with_generation=False)
    with pytest.raises(qt.TemplateError, match="no promoted generation record"):
        rb.resolve("dev-silo", layout=layout)


def test_record_activation_change_detection(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    run_dir = str(tmp_path / "run")
    assert rb.record_activation(layout, "dev-silo", GEN, run_status_dir=run_dir) is True
    # status file written
    assert os.path.isfile(os.path.join(run_dir, "dev-silo"))
    # second time, same generation -> not a new activation
    assert rb.record_activation(layout, "dev-silo", GEN, run_status_dir=run_dir) is False
    # new generation -> activation change again
    gen2 = "sha256:" + "b" * 64
    assert rb.record_activation(layout, "dev-silo", gen2, run_status_dir=run_dir) is True


def test_main_untemplated_exit_3(tmp_path, monkeypatch, capsys):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    assert rb.main(["dev-silo"]) == 3


def test_main_prints_digest(tmp_path, monkeypatch, capsys):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    assert rb.main(["dev-silo"]) == 0
    assert capsys.readouterr().out.strip() == GEN


def test_main_tag_binding_exit_2(tmp_path, monkeypatch):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo", active="latest")
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    assert rb.main(["dev-silo"]) == 2
