"""Unit tests for the template on-disk model (todo/fableplan task 01).

Covers the trust-boundary guarantees: TOML round-trips, a mutable tag
reference is rejected anywhere a generation is referenced, and a binding
write is atomic (temp + rename, never in-place)."""
from __future__ import annotations

import os

import pytest

import qdistro_templates as qt


# --------------------------------------------------------------------------
# TOML round-trip
# --------------------------------------------------------------------------

def test_toml_round_trip_scalars_arrays_tables():
    obj = {
        "silo": "dev-silo",
        "template": "tier2-dev",
        "identity_revision": 3,
        "previous_generations": [
            "sha256:" + "a" * 64,
            "sha256:" + "b" * 64,
        ],
        "template_table": {"class": "derived", "enforced": "true"},
    }
    text = qt.dumps_toml(obj)
    assert qt.loads_toml(text) == obj


def test_toml_round_trip_array_of_tables():
    obj = {
        "name": "tier2-dev",
        "probe": [
            {"name": "process-starts", "kind": "process", "timeout": 30},
            {"name": "gcc", "kind": "command", "command": "gcc --version"},
        ],
    }
    assert qt.loads_toml(qt.dumps_toml(obj)) == obj


def test_toml_empty_array_round_trips():
    obj = {"previous_generations": []}
    assert qt.loads_toml(qt.dumps_toml(obj)) == obj


def test_toml_string_escaping():
    obj = {"k": 'a "quoted" \\ path\nwith newline'}
    assert qt.loads_toml(qt.dumps_toml(obj)) == obj


def test_toml_control_char_escaping_round_trips():
    # NUL, backspace, vertical tab, form feed, DEL — all forbidden raw in
    # a TOML basic string; the emitter must escape them.
    obj = {"k": "x\x00y\x08z\x0bq\x0cr\x7fs"}
    text = qt.dumps_toml(obj)
    assert qt.loads_toml(text) == obj


# --------------------------------------------------------------------------
# digest / tag rejection
# --------------------------------------------------------------------------

def test_is_digest_accepts_sha256():
    assert qt.is_digest("sha256:" + "0" * 64)


@pytest.mark.parametrize("bad", [
    "latest",
    "qdistro/tier2-dev:latest",
    "sha256:tooshort",
    "sha256:" + "g" * 64,        # non-hex
    "sha256:" + "a" * 63,        # wrong length
    "" + "a" * 64,               # missing prefix
    12345,
    None,
])
def test_is_digest_rejects_tags_and_junk(bad):
    assert not qt.is_digest(bad)


@pytest.mark.cheat_aware(
    protects="a binding can never resolve a mutable tag to a launch target",
    severity="critical",
    cheats=["loosen DIGEST_RE", "accept :latest", "skip require_digest in read_binding"],
    consequence="a failed candidate could become the user-visible launch target",
)
def test_binding_rejects_tag_active_generation():
    binding = _valid_binding(active_generation="qdistro/tier2-dev:latest")
    with pytest.raises(qt.TemplateError, match="immutable sha256"):
        qt.validate_binding(binding)


def _valid_binding(**over):
    binding = {
        "silo": "dev-silo",
        "template": "tier2-dev",
        "backend": "podman-image",
        "active_generation": "sha256:" + "c" * 64,
        "previous_generations": ["sha256:" + "d" * 64],
        "state_path": "/var/lib/qdistro/silos/dev-silo/state",
        "activation_policy": "manual",
        "identity_revision": 2,
    }
    binding.update(over)
    return binding


def test_binding_rejects_tag_in_previous_generations():
    binding = _valid_binding(
        active_generation="sha256:" + "a" * 64,
        previous_generations=["sha256:" + "b" * 64, "tier2-dev:old"],
    )
    with pytest.raises(qt.TemplateError, match="previous_generations"):
        qt.validate_binding(binding)


def test_binding_accepts_digest():
    binding = _valid_binding()
    assert qt.validate_binding(binding) is binding


def test_binding_rejects_unknown_backend():
    with pytest.raises(qt.TemplateError, match="unsupported"):
        qt.validate_binding(_valid_binding(backend="vm-artifact"))


def test_binding_requires_absolute_state_path():
    with pytest.raises(qt.TemplateError, match="state_path"):
        qt.validate_binding(_valid_binding(state_path="relative/state"))
    with pytest.raises(qt.TemplateError, match="missing required key"):
        b = _valid_binding()
        del b["state_path"]
        qt.validate_binding(b)


def test_binding_rejects_bad_activation_policy():
    with pytest.raises(qt.TemplateError, match="activation_policy"):
        qt.validate_binding(_valid_binding(activation_policy="yolo"))


def test_binding_requires_integer_identity_revision():
    with pytest.raises(qt.TemplateError, match="identity_revision"):
        qt.validate_binding(_valid_binding(identity_revision="2"))
    # bool is an int subclass — a TOML `true` must not pass as a revision.
    with pytest.raises(qt.TemplateError, match="identity_revision"):
        qt.validate_binding(_valid_binding(identity_revision=True))


def test_retention_rejects_bool_counts():
    base = {
        "keep_promoted_generations": 3,
        "keep_promoted_generations_vm": 2,
        "failed_candidate_days": 7,
        "build_log_days": 180,
        "audit_evidence_years": 3,
    }
    with pytest.raises(qt.TemplateError):
        qt.validate_retention(dict(base, keep_promoted_generations_vm=True))


def _valid_manifest(**over):
    base = {
        "template": "tier2-dev",
        "run_id": "r1",
        "image_digest": "sha256:" + "a" * 64,
        "image_id": "sha256:" + "b" * 64,
        "containerfile_digest": "sha256:" + "c" * 64,
        "build_command": "podman build .",
        "network_mode": "unrestricted",
        "artifact_manifest": [],
        "generation_ref": "sha256:" + "a" * 64,
    }
    base.update(over)
    return base


def test_manifest_requires_digests():
    assert qt.validate_manifest(_valid_manifest()) is not None
    with pytest.raises(qt.TemplateError):
        qt.validate_manifest(_valid_manifest(image_digest="latest"))


def test_manifest_requires_artifact_manifest():
    m = _valid_manifest()
    del m["artifact_manifest"]
    with pytest.raises(qt.TemplateError, match="artifact_manifest"):
        qt.validate_manifest(m)


def test_manifest_requires_generation_ref_digest():
    m = _valid_manifest()
    del m["generation_ref"]
    with pytest.raises(qt.TemplateError, match="generation_ref"):
        qt.validate_manifest(m)
    with pytest.raises(qt.TemplateError, match="generation_ref"):
        qt.validate_manifest(_valid_manifest(generation_ref="latest"))
    assert qt.generation_ref(_valid_manifest()) == "sha256:" + "a" * 64


def test_require_safe_name_rejects_path_escape():
    for bad in ["../evil", "a/b", "..", ".hidden", "", "a b", "a:b"]:
        with pytest.raises(qt.TemplateError):
            qt.require_safe_name(bad, "template")
    for ok in ["tier2-dev", "dev-silo", "a", "A1_b.c", "20260610T100013Z-c2cb2019"]:
        assert qt.require_safe_name(ok) == ok


def test_layout_rejects_unsafe_template_name(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    with pytest.raises(qt.TemplateError):
        layout.template_policy("../../etc/passwd")
    with pytest.raises(qt.TemplateError):
        layout.candidate_dir("tier2-dev", "../escape")


def test_manifest_validation_section_shape_checked_when_present():
    qt.validate_manifest(_valid_manifest(validation={"command": "probe-all"}))
    with pytest.raises(qt.TemplateError, match="validation"):
        qt.validate_manifest(_valid_manifest(validation={"no_command": 1}))


def test_pin_validation():
    pin = {
        "owner_type": "silo",
        "owner_id": "dev-silo",
        "reason": "active",
        "generation": "sha256:" + "a" * 64,
        "template": "tier2-dev",
    }
    assert qt.validate_pin(dict(pin)) is not None
    with pytest.raises(qt.TemplateError, match="pin.reason"):
        qt.validate_pin(dict(pin, reason="bogus"))


def test_template_policy_validation():
    policy = {
        "template": {
            "class": "derived",
            "state_boundary": {"class": "recipe-derived-toolchain", "enforced": "true"},
        }
    }
    assert qt.validate_template_policy(policy) is policy
    with pytest.raises(qt.TemplateError):
        qt.validate_template_policy({"template": {"class": "nonsense",
                                                  "state_boundary": {"enforced": "true"}}})


# --------------------------------------------------------------------------
# atomic write-rename
# --------------------------------------------------------------------------

def test_write_binding_is_atomic_rename(tmp_path, monkeypatch):
    """The binding must be written via a temp file + rename, never edited
    in place: a reader either sees the whole old file or the whole new one.

    We prove the rename by intercepting os.replace and asserting the temp
    source exists and the destination does not yet at replace time."""
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    path = layout.binding_file("dev-silo")

    binding = _valid_binding(active_generation="sha256:" + "a" * 64,
                             previous_generations=[])
    qt.write_binding(path, binding)
    first = qt.read_binding(path)

    seen = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        seen["src_existed"] = os.path.exists(src)
        seen["dst_existed_before"] = os.path.exists(dst)
        seen["src_is_tmp"] = os.path.basename(src).startswith(".tmp-")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    binding2 = dict(binding, active_generation="sha256:" + "f" * 64)
    qt.write_binding(path, binding2)

    assert seen["src_is_tmp"], "binding must be written to a .tmp- file first"
    assert seen["src_existed"], "temp source must exist at rename time"
    assert seen["dst_existed_before"], "old binding stays until the atomic rename"
    assert qt.read_binding(path)["active_generation"] == "sha256:" + "f" * 64
    # No leftover temp files in the bindings dir.
    leftovers = [n for n in os.listdir(layout.bindings_dir) if n.startswith(".tmp-")]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    assert first["active_generation"] == "sha256:" + "a" * 64


def test_ensure_skeleton_idempotent_and_modes(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    qt.ensure_skeleton(layout)  # second call must not error
    assert os.path.isdir(layout.bindings_dir)
    assert os.path.isdir(layout.pins_dir)
    assert os.path.isdir(layout.identity_dir)
    # Security state dirs are owner-only.
    assert (os.stat(layout.bindings_dir).st_mode & 0o777) == 0o700
    assert (os.stat(layout.pins_dir).st_mode & 0o777) == 0o700


def test_shipped_retention_defaults_validate():
    """The retention file the bootstrap installs must pass validation,
    including the VM-artifact count."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(repo, "deploy", "etc", "qdistro", "template-retention.toml")
    retention = qt.read_toml(path)
    assert qt.validate_retention(retention) is not None
    assert retention["keep_promoted_generations_vm"] == 2


def test_retention_requires_vm_count():
    base = {
        "keep_promoted_generations": 3,
        "keep_promoted_generations_vm": 2,
        "failed_candidate_days": 7,
        "build_log_days": 180,
        "audit_evidence_years": 3,
    }
    assert qt.validate_retention(dict(base)) is not None
    del base["keep_promoted_generations_vm"]
    with pytest.raises(qt.TemplateError, match="keep_promoted_generations_vm"):
        qt.validate_retention(base)


def test_shipped_example_policy_and_binding_validate():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    policy = qt.read_toml(os.path.join(repo, "templates", "examples", "tier2-dev.toml"))
    assert qt.validate_template_policy(policy) is not None
    binding = qt.read_toml(os.path.join(repo, "templates", "examples", "binding-dev-silo.toml"))
    assert qt.validate_binding(binding) is not None


def test_candidate_state_marker(tmp_path):
    layout = qt.Layout(var=str(tmp_path / "var"))
    cdir = layout.candidate_dir("tier2-dev", "run-1")
    os.makedirs(cdir)
    assert qt.candidate_state(cdir) is None
    qt.set_candidate_state(cdir, "built")
    assert qt.candidate_state(cdir) == "built"
    qt.set_candidate_state(cdir, "validated")
    assert qt.candidate_state(cdir) == "validated"
    with pytest.raises(qt.TemplateError):
        qt.set_candidate_state(cdir, "bogus")
