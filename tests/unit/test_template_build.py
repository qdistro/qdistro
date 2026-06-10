"""Unit tests for qdistro-template-build (todo/fableplan task 02).

Covers manifest writing, run-id uniqueness, containerfile resolution,
network-mode honesty, and the build orchestration's success/failure
side effects with podman stubbed out. A real tier2-dev podman build is
exercised separately (rootless podman smoke + the VM bats suite)."""
from __future__ import annotations

import os

import pytest

import qdistro_templates as qt
import qdistro_template_build as build


def _policy(tmp_path, *, network_mode="unrestricted", cls="derived",
            containerfile="Containerfile.tier2-dev"):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    policy = {
        "template": {
            "class": cls,
            "state_boundary": {"class": "recipe-derived-toolchain", "enforced": "true"},
            "build": {"containerfile": containerfile, "network_mode": network_mode},
        }
    }
    qt.write_toml_atomic(layout.template_policy("tier2-dev"), policy, 0o644)
    return layout, policy


def test_run_id_unique_and_shaped():
    a, b = build.make_run_id(), build.make_run_id()
    assert a != b
    assert a.endswith(tuple("0123456789abcdef"))
    assert "T" in a and a.endswith(a.split("-")[-1])


def test_resolve_containerfile_absolute(tmp_path):
    cf = tmp_path / "Containerfile.x"
    cf.write_text("FROM scratch\n")
    policy = {"template": {"build": {"containerfile": str(cf)}}}
    assert build.resolve_containerfile(policy) == str(cf)


def test_resolve_containerfile_missing(tmp_path):
    policy = {"template": {"build": {"containerfile": "/nonexistent/Containerfile.q"}}}
    with pytest.raises(qt.TemplateError, match="not found"):
        build.resolve_containerfile(policy)


def test_resolve_containerfile_shipped_recipe_exists():
    # The shipped tier2-dev recipe must resolve via the in-tree recipes dir.
    policy = {"template": {"build": {"containerfile": "Containerfile.tier2-dev"}}}
    resolved = build.resolve_containerfile(policy)
    assert resolved.endswith("recipes/Containerfile.tier2-dev")
    assert os.path.isfile(resolved)


# --------------------------------------------------------------------------
# build context resolution (fableplan2 task 02)
# --------------------------------------------------------------------------

def test_resolve_build_context_default_is_containerfile_dir(tmp_path):
    cf = str(tmp_path / "recipes" / "Containerfile.x")
    policy = {"template": {"build": {"containerfile": "Containerfile.x"}}}
    assert build.resolve_build_context(policy, cf) == os.path.dirname(cf)


def test_resolve_build_context_absolute_within_allowed(tmp_path, monkeypatch):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    monkeypatch.setattr(build, "CONTEXT_DIRS", (str(tmp_path),))
    policy = {"template": {"build": {"containerfile": "c", "context": str(ctx)}}}
    assert build.resolve_build_context(policy, "/x/c") == str(ctx)


def test_resolve_build_context_absolute_outside_allowed_rejected(tmp_path, monkeypatch):
    # An absolute context outside the allowlisted roots must be refused — it
    # would let a policy sweep arbitrary host files into the empty-room build.
    ctx = tmp_path / "evil"
    ctx.mkdir()
    monkeypatch.setattr(build, "CONTEXT_DIRS", ("/usr/lib/qdistro",))
    monkeypatch.setattr(build, "RECIPES_DIRS", ("/usr/lib/qdistro/templates/recipes",))
    policy = {"template": {"build": {"containerfile": "c", "context": str(ctx)}}}
    with pytest.raises(qt.TemplateError, match="outside the allowed roots"):
        build.resolve_build_context(policy, "/x/c")


def test_resolve_build_context_absolute_missing(tmp_path):
    policy = {"template": {"build": {"containerfile": "c",
                                     "context": str(tmp_path / "nope")}}}
    with pytest.raises(qt.TemplateError, match="not a directory"):
        build.resolve_build_context(policy, "/x/c")


def test_resolve_build_context_relative_searches_bases(tmp_path, monkeypatch):
    base = tmp_path / "base"
    (base / "tier2").mkdir(parents=True)
    monkeypatch.setattr(build, "CONTEXT_DIRS", (str(base),))
    policy = {"template": {"build": {"containerfile": "c", "context": "tier2"}}}
    assert build.resolve_build_context(policy, "/x/c") == str(base / "tier2")


def test_resolve_build_context_relative_unsafe_rejected(tmp_path):
    policy = {"template": {"build": {"containerfile": "c", "context": "../etc"}}}
    with pytest.raises(qt.TemplateError):
        build.resolve_build_context(policy, "/x/c")


def test_shipped_browser_recipe_and_context_resolve():
    # The tier2-browser recipe resolves under the recipes dir and its
    # context = "tier2" resolves to the in-tree tier2/ asset dir, which must
    # carry the SHARED entrypoint.sh + weston.ini the recipe COPYs.
    policy = qt.validate_template_policy(
        qt.read_toml(_repo_path("templates/examples/tier2-browser.toml")))
    cf = build.resolve_containerfile(policy)
    assert cf.endswith("recipes/Containerfile.tier2-browser") and os.path.isfile(cf)
    ctx = build.resolve_build_context(policy, cf)
    assert os.path.isfile(os.path.join(ctx, "weston.ini"))
    assert os.path.isfile(os.path.join(ctx, "entrypoint.sh"))


def _repo_path(rel):
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo, rel)


def test_shipped_browser_policy_validates_split_app_profile():
    # fableplan2 task 02: the browser is split-app-profile / enforced=partial,
    # and declares the page-open probe gate.
    policy = qt.read_toml(_repo_path("templates/examples/tier2-browser.toml"))
    assert qt.validate_template_policy(policy) is not None
    tmpl = policy["template"]
    assert tmpl["state_boundary"]["class"] == "split-app-profile"
    assert tmpl["state_boundary"]["enforced"] == "partial"
    assert tmpl["activation_snapshot"] == "strict"
    probe_kinds = {p["kind"] for p in tmpl["probe"]}
    assert "page-open" in probe_kinds
    assert all(p["required"] for p in tmpl["probe"])
    # TIER2_NETWORK is task-04's launch registry, NOT the binding/policy.
    assert "network" not in tmpl and "network_mode" in tmpl["build"]
    # The identity selector example carries the declared chromium package.
    sel = qt.read_toml(_repo_path("templates/examples/identity-chromium.toml"))
    assert sel["identity"]["executable"]["expected_package"] == "chromium"


def test_file_digest(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"hello")
    # sha256("hello")
    assert build.file_digest(str(p)) == (
        "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_declared_network_mode_rejects_record(tmp_path):
    _, policy = _policy(tmp_path, network_mode="record")
    with pytest.raises(qt.TemplateError, match="recording proxy"):
        build.declared_network_mode(policy)


def test_declared_network_mode_unrestricted_ok(tmp_path):
    _, policy = _policy(tmp_path)
    assert build.declared_network_mode(policy) == "unrestricted"


def test_write_candidate_manifest_round_trips(tmp_path):
    layout = qt.Layout(var=str(tmp_path / "var"))
    cdir = layout.candidate_dir("tier2-dev", "run-1")
    os.makedirs(cdir)
    manifest = build.write_candidate_manifest(
        cdir, template="tier2-dev", run_id="run-1",
        image_digest="sha256:" + "a" * 64, image_id="sha256:" + "b" * 64,
        containerfile="/x/Containerfile.tier2-dev",
        containerfile_digest="sha256:" + "c" * 64,
        build_command="podman build ...", network_mode="unrestricted",
    )
    on_disk = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    assert on_disk == manifest
    assert on_disk["inputs"][0]["path"] == "Containerfile.tier2-dev"
    assert on_disk["artifact_manifest"] == []
    # The launch reference is the image_id, recorded explicitly.
    assert on_disk["generation_ref"] == "sha256:" + "b" * 64
    assert qt.generation_ref(on_disk) == "sha256:" + "b" * 64


def test_build_failure_leaves_evidence_and_no_manifest(tmp_path, monkeypatch):
    layout, _ = _policy(tmp_path)
    # Point the recipe resolver at a real file so resolution succeeds.
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))

    class FakeProc:
        returncode = 7

    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: FakeProc())
    rc = build.build("tier2-dev", layout=layout)
    assert rc == 1
    # exactly one candidate dir, marked failed, with a build log, no manifest
    cands = os.listdir(layout.candidates_dir("tier2-dev"))
    assert len(cands) == 1
    cdir = layout.candidate_dir("tier2-dev", cands[0])
    assert qt.candidate_state(cdir) == "failed"
    assert os.path.isfile(os.path.join(cdir, "build.log"))
    assert not os.path.exists(os.path.join(cdir, "manifest.toml"))


def test_build_success_writes_manifest_and_marks_built(tmp_path, monkeypatch):
    layout, _ = _policy(tmp_path)
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: FakeProc())
    # Mirror real podman: {{.Id}} is bare hex, {{.Digest}} is sha256-prefixed.
    monkeypatch.setattr(build, "_podman_inspect", lambda tag, fmt: (
        "sha256:" + ("d" * 64) if "Digest" in fmt else "e" * 64
    ))
    rc = build.build("tier2-dev", layout=layout)
    assert rc == 0
    cands = os.listdir(layout.candidates_dir("tier2-dev"))
    assert len(cands) == 1
    cdir = layout.candidate_dir("tier2-dev", cands[0])
    assert qt.candidate_state(cdir) == "built"
    manifest = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    assert manifest["image_id"] == "sha256:" + "e" * 64
    assert manifest["image_digest"] == "sha256:" + "d" * 64
    assert manifest["network_mode"] == "unrestricted"


def test_normalize_digest():
    assert build._normalize_digest("e" * 64) == "sha256:" + "e" * 64
    assert build._normalize_digest("sha256:" + "e" * 64) == "sha256:" + "e" * 64


def test_build_success_records_generation_ref(tmp_path, monkeypatch):
    layout, _ = _policy(tmp_path)
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))
    monkeypatch.setattr(build.subprocess, "run",
                        lambda *a, **k: type("P", (), {"returncode": 0})())
    monkeypatch.setattr(build, "_podman_inspect", lambda tag, fmt: (
        "sha256:" + ("d" * 64) if "Digest" in fmt else "e" * 64
    ))
    assert build.build("tier2-dev", layout=layout) == 0
    cdir = layout.candidate_dir("tier2-dev", os.listdir(layout.candidates_dir("tier2-dev"))[0])
    manifest = qt.read_manifest(os.path.join(cdir, "manifest.toml"))
    assert manifest["generation_ref"] == "sha256:" + "e" * 64


def test_clean_env_drops_credentials(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy:3128")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/ssh-agent")
    monkeypatch.setenv("REGISTRY_AUTH_FILE", "/run/auth.json")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "shh")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/admin")
    env = build._clean_env()
    assert "PATH" in env and "HOME" in env
    for leaked in ("HTTPS_PROXY", "SSH_AUTH_SOCK", "REGISTRY_AUTH_FILE",
                   "AWS_SECRET_ACCESS_KEY"):
        assert leaked not in env, f"{leaked} must not reach the build env"


def test_build_passes_http_proxy_false_and_clean_env(tmp_path, monkeypatch):
    layout, _ = _policy(tmp_path)
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))
    captured = {}

    def fake_run(cmd, *a, **k):
        if "build" in cmd:  # capture the build invocation, not the later untag
            captured["cmd"] = cmd
            captured["env"] = k.get("env")
        return type("P", (), {"returncode": 0})()

    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy:3128")
    monkeypatch.setattr(build.subprocess, "run", fake_run)
    monkeypatch.setattr(build, "_podman_inspect", lambda tag, fmt: (
        "sha256:" + ("d" * 64) if "Digest" in fmt else "e" * 64
    ))
    assert build.build("tier2-dev", layout=layout) == 0
    assert "--http-proxy=false" in captured["cmd"]
    assert "HTTPS_PROXY" not in (captured["env"] or {})


def test_build_marks_failed_when_podman_missing(tmp_path, monkeypatch):
    layout, _ = _policy(tmp_path)
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))

    def boom(*a, **k):
        raise FileNotFoundError("podman")

    monkeypatch.setattr(build.subprocess, "run", boom)
    rc = build.build("tier2-dev", layout=layout)
    assert rc == 1
    cdir = layout.candidate_dir("tier2-dev", os.listdir(layout.candidates_dir("tier2-dev"))[0])
    assert qt.candidate_state(cdir) == "failed"
    assert not os.path.exists(os.path.join(cdir, "manifest.toml"))


def test_build_rejects_unsafe_template_name(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    # main() catches the TemplateError from the name guard and returns 2.
    assert build.main(["../../etc/passwd"]) == 2
    # build() raises through Layout's name guard before any I/O.
    with pytest.raises(qt.TemplateError):
        build.build("../escape", layout=layout)


def test_build_runs_twice_distinct_run_ids(tmp_path, monkeypatch):
    layout, _ = _policy(tmp_path)
    cf = tmp_path / "Containerfile.tier2-dev"
    cf.write_text("FROM scratch\n")
    monkeypatch.setattr(build, "RECIPES_DIRS", (str(tmp_path),))

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: FakeProc())
    monkeypatch.setattr(build, "_podman_inspect", lambda tag, fmt: (
        "sha256:" + ("d" * 64) if "Digest" in fmt else "sha256:" + ("e" * 64)
    ))
    assert build.build("tier2-dev", layout=layout) == 0
    assert build.build("tier2-dev", layout=layout) == 0
    cands = os.listdir(layout.candidates_dir("tier2-dev"))
    assert len(cands) == 2, "two builds must produce two distinct candidate run-ids"


def test_build_rejects_non_derived(tmp_path):
    layout, _ = _policy(tmp_path, cls="artifact")
    assert build.build("tier2-dev", layout=layout) == 2


def test_build_missing_policy(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    assert build.build("ghost", layout=layout) == 2
