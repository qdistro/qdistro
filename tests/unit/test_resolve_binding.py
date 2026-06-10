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


def test_resolve_unreadable_bindings_dir_is_hard_error(tmp_path):
    # An unreadable bindings dir (EACCES) must NOT map to rc 3 (untemplated) —
    # that would fail-open to the mutable :latest tag at the launch boundary.
    # Only a genuinely missing binding file is untemplated.
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    os.chmod(layout.bindings_dir, 0o000)
    try:
        with pytest.raises(OSError):
            rb.resolve("dev-silo", layout=layout)
    finally:
        # Restore so pytest's tmp_path cleanup can recurse into the dir.
        os.chmod(layout.bindings_dir, 0o700)


def test_main_unreadable_bindings_dir_exit_2(tmp_path, monkeypatch):
    # main() must turn the permission error into rc 2 (hard error) with a
    # FATAL log line, not a traceback and not rc 3.
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    os.chmod(layout.bindings_dir, 0o000)
    try:
        assert rb.main(["dev-silo"]) == 2
    finally:
        os.chmod(layout.bindings_dir, 0o700)


# --------------------------------------------------------------------------
# --launch-env (fableplan2 task 01): one binding read → the launch path's
# full input set, no second read that could race a concurrent promote.
# --------------------------------------------------------------------------

def test_launch_env_emits_all_fields(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    rc, env = rb.compute_launch_env("dev-silo", layout=layout)
    assert rc == 0
    assert env["generation"] == GEN
    assert env["template"] == "tier2-dev"
    assert env["state_path"] == "/var/lib/qdistro/silos/dev-silo/state"
    # No prior marker → this is the first activation.
    assert env["first_activation"] is True


def test_launch_env_is_side_effect_free(tmp_path):
    # compute_launch_env must NOT write the marker (task 05 commits it only
    # after the pre-activation snapshot succeeds).
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    rb.compute_launch_env("dev-silo", layout=layout)
    assert not os.path.exists(rb.activated_marker(layout, "dev-silo"))
    # And still reports first_activation on the next call (marker not written).
    _, env = rb.compute_launch_env("dev-silo", layout=layout)
    assert env["first_activation"] is True


def test_launch_env_first_activation_flips_after_marker(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    rb.record_activation(layout, "dev-silo", GEN,
                         run_status_dir=str(tmp_path / "run"))
    _, env = rb.compute_launch_env("dev-silo", layout=layout)
    assert env["first_activation"] is False


def test_launch_env_reads_binding_exactly_once(tmp_path, monkeypatch):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    calls = {"n": 0}
    real = qt.read_binding

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(qt, "read_binding", counting)
    rb.compute_launch_env("dev-silo", layout=layout)
    assert calls["n"] == 1


def test_launch_env_untemplated_returns_3(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    rc, env = rb.compute_launch_env("dev-silo", layout=layout)
    assert rc == 3 and env is None


def test_launch_env_tag_binding_is_hard_error(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo", active="qdistro/tier2-dev:latest")
    with pytest.raises(qt.TemplateError):
        rb.compute_launch_env("dev-silo", layout=layout)


def test_main_launch_env_prints_keyvalues(tmp_path, monkeypatch, capsys):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    assert rb.main(["dev-silo", "--launch-env"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["GENERATION"] == GEN
    assert out["TEMPLATE"] == "tier2-dev"
    assert out["STATE_PATH"] == "/var/lib/qdistro/silos/dev-silo/state"
    assert out["FIRST_ACTIVATION"] == "yes"
    # Without --record the marker is not committed.
    assert not os.path.exists(rb.activated_marker(layout, "dev-silo"))


def test_main_launch_env_with_record_commits_marker(tmp_path, monkeypatch, capsys):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo")
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    # RUN_STATUS_DIR is bound from the env at import time, so an env var here
    # is ineffective — set the module attribute directly (and monkeypatch
    # restores it, so this test never pollutes others).
    monkeypatch.setattr(rb, "RUN_STATUS_DIR", str(tmp_path / "run"))
    assert rb.main(["dev-silo", "--record", "--launch-env"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["FIRST_ACTIVATION"] == "yes"
    assert os.path.isfile(rb.activated_marker(layout, "dev-silo"))


def test_main_launch_env_untemplated_exit_3(tmp_path, monkeypatch):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    assert rb.main(["dev-silo", "--launch-env"]) == 3


def test_main_launch_env_tag_binding_exit_2(tmp_path, monkeypatch):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    _binding(layout, "dev-silo", active="latest")
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    assert rb.main(["dev-silo", "--launch-env"]) == 2
