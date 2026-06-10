"""Unit tests for qdistro-template-promote (todo/fableplan task 04).

The binding flip, gating, identity-class-change abort, rollback, and
crash consistency are tested with the identity resolver stubbed; a real
build→validate→promote→launch chain is exercised by the rootless-podman
smoke + the VM bats suite."""
from __future__ import annotations

import os
import subprocess

import pytest

import qdistro_templates as qt
import qdistro_template_promote as promote


GEN_A = "sha256:" + "a" * 64
GEN_B = "sha256:" + "b" * 64


def _layout(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    return layout


def _validated_candidate(layout, template, run_id, gen, *, state="validated",
                         report_result="validated", failed_required=False):
    cdir = layout.candidate_dir(template, run_id)
    os.makedirs(os.path.join(cdir, "evidence"))
    manifest = {
        "template": template, "run_id": run_id,
        "image_digest": gen, "image_id": gen, "containerfile_digest": GEN_A,
        "build_command": "podman build ...", "network_mode": "unrestricted",
        "artifact_manifest": [], "generation_ref": gen,
    }
    qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"), manifest, 0o644)
    report = {
        "run_id": run_id, "template": template, "generation_ref": gen,
        "result": report_result, "checks_total": 1,
        "checks_failed": 1 if failed_required else 0,
        "check": [{"name": "p", "kind": "process", "class": "local-runtime",
                   "required": True, "result": "fail" if failed_required else "pass",
                   "duration_seconds": 0.1, "reason": ""}],
    }
    qt.write_toml_atomic(os.path.join(cdir, "evidence", "validation.toml"), report, 0o644)
    qt.set_candidate_state(cdir, state)
    return cdir


def _no_resolver(image_ref, selector):
    raise AssertionError("resolver should not be called without selectors")


def _identity(layout, silo, app, expected_package):
    idir = layout.identity_for(silo)
    os.makedirs(idir, exist_ok=True)
    qt.write_toml_atomic(os.path.join(idir, f"{app}.toml"), {
        "identity": {"executable": {
            "path_in_template": "/usr/bin/tool",
            "expected_package": expected_package,
            "selinux_type": "qdistro_tool_t",
        }}
    }, 0o644)


# --------------------------------------------------------------------------
# gating
# --------------------------------------------------------------------------

def test_promote_refuses_non_validated(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-built", GEN_A, state="built")
    rc = promote.promote("dev-silo", "run-built", layout=layout, resolver=_no_resolver)
    assert rc == 1
    assert not os.path.isfile(layout.binding_file("dev-silo"))
    assert not os.path.isdir(layout.generations_dir("tier2-dev"))


def test_promote_refuses_failed_required_report(tmp_path):
    layout = _layout(tmp_path)
    # state says validated but the report has a failed required check.
    _validated_candidate(layout, "tier2-dev", "run-x", GEN_A,
                         report_result="failed", failed_required=True)
    rc = promote.promote("dev-silo", "run-x", layout=layout, resolver=_no_resolver)
    assert rc == 1
    assert not os.path.isfile(layout.binding_file("dev-silo"))


def test_promote_missing_candidate(tmp_path):
    layout = _layout(tmp_path)
    assert promote.promote("dev-silo", "nope", layout=layout) == 2


def test_promote_refuses_when_report_missing(tmp_path):
    layout = _layout(tmp_path)
    cdir = _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    os.unlink(os.path.join(cdir, "evidence", "validation.toml"))
    rc = promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver)
    assert rc == 1
    assert not os.path.isfile(layout.binding_file("dev-silo"))


def test_promote_refuses_mismatched_report(tmp_path):
    layout = _layout(tmp_path)
    cdir = _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    # report belongs to a different generation than the manifest
    rpath = os.path.join(cdir, "evidence", "validation.toml")
    report = qt.read_toml(rpath)
    report["generation_ref"] = GEN_B
    qt.write_toml_atomic(rpath, report, 0o644)
    rc = promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver)
    assert rc == 1
    assert not os.path.isfile(layout.binding_file("dev-silo"))


def test_main_rejects_both_modes():
    # argparse error exits nonzero via SystemExit.
    with pytest.raises(SystemExit):
        promote.main(["dev-silo", "run-a", "--rollback", GEN_A])


def _refused_rows(layout):
    import qdistro_template_audit as audit
    db = os.path.join(layout.var, "audit", "template_audit.sqlite")
    log = audit.TemplateAuditLog(db)
    try:
        return [r for r in log.recent() if r["event"] == "template.promote.refused"]
    finally:
        log.close()


def test_resolver_timeout_refuses_with_audit_row(tmp_path):
    # A hung identity probe must fail closed through the refusal path (nonzero
    # + audited promote.refused), never escape as an unhandled traceback.
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _identity(layout, "dev-silo", "tool", expected_package="tool")

    def hangs(image_ref, selector):
        raise subprocess.TimeoutExpired(cmd="podman run", timeout=60)

    rc = promote.promote("dev-silo", "run-a", layout=layout, resolver=hangs)
    assert rc == 1
    assert not os.path.isfile(layout.binding_file("dev-silo"))
    assert _refused_rows(layout), "a probe timeout must record a promote.refused row"


def test_resolve_selector_timeout_raises_template_error(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="podman run", timeout=60)
    monkeypatch.setattr(promote.subprocess, "run", boom)
    with pytest.raises(qt.TemplateError):
        promote.resolve_selector("img", {"executable": {"path_in_template": "/usr/bin/tool"}})


def test_resolve_selector_oserror_raises_template_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("podman")
    monkeypatch.setattr(promote.subprocess, "run", boom)
    with pytest.raises(qt.TemplateError):
        promote.resolve_selector("img", {"executable": {"path_in_template": "/usr/bin/tool"}})


def test_state_path_refused_on_existing_binding(tmp_path):
    # --state-path may only be chosen on the first promote; once a binding
    # exists it must be refused (side-effect-free), never rewrite the silo's
    # only path to real state.
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _validated_candidate(layout, "tier2-dev", "run-b", GEN_B)
    # promote now creates the state tree on first promote, so the override
    # must point somewhere creatable (under the test root), not a bare /custom.
    custom = str(tmp_path / "custom-state")
    other = str(tmp_path / "other-state")
    assert promote.promote("dev-silo", "run-a", layout=layout,
                           resolver=_no_resolver,
                           state_path=custom) == 0
    before = qt.read_binding(layout.binding_file("dev-silo"))
    assert before["state_path"] == custom
    assert os.path.isdir(custom), "first promote materialized the state tree"
    rc = promote.promote("dev-silo", "run-b", layout=layout, resolver=_no_resolver,
                         state_path=other)
    assert rc == 1
    after = qt.read_binding(layout.binding_file("dev-silo"))
    assert after["active_generation"] == GEN_A, "binding unchanged"
    assert after["state_path"] == custom, "state_path not rewritten"
    assert not os.path.exists(other), "refused override created no state tree"
    assert _refused_rows(layout), "a --state-path override must record a refused row"


# --------------------------------------------------------------------------
# first + second promote
# --------------------------------------------------------------------------

def test_first_promote_creates_binding_and_active_pin(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    rc = promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver)
    assert rc == 0
    binding = qt.read_binding(layout.binding_file("dev-silo"))
    assert binding["active_generation"] == GEN_A
    assert binding["previous_generations"] == []
    assert binding["identity_revision"] == 1
    # Default state_path honors the layout var root (not a hardcoded
    # /var/lib literal) and the state tree was materialized.
    assert binding["state_path"] == layout.default_state_path("dev-silo")
    assert os.path.isdir(binding["state_path"])
    meta = qt.read_state_meta(binding["state_path"])
    assert meta and meta["mechanism"] in qt.STATE_MECHANISMS
    # generation record materialized
    assert os.path.isfile(os.path.join(
        layout.generation_dir("tier2-dev", GEN_A), "manifest.toml"))
    # active pin present
    assert os.path.isfile(os.path.join(
        layout.pins_for("tier2-dev", GEN_A), "active.toml"))


def test_second_promote_rolls_outgoing_into_previous(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _validated_candidate(layout, "tier2-dev", "run-b", GEN_B)
    assert promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver) == 0
    assert promote.promote("dev-silo", "run-b", layout=layout, resolver=_no_resolver) == 0
    binding = qt.read_binding(layout.binding_file("dev-silo"))
    assert binding["active_generation"] == GEN_B
    assert binding["previous_generations"] == [GEN_A]
    assert binding["identity_revision"] == 2
    # outgoing A: active pin gone, rollback-window pin present
    assert not os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_A), "active.toml"))
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_A), "rollback-window.toml"))
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_B), "active.toml"))


def test_promote_same_generation_refused(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    assert promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver) == 0
    # promote the same digest again (a second validated candidate, same gen)
    _validated_candidate(layout, "tier2-dev", "run-a2", GEN_A)
    assert promote.promote("dev-silo", "run-a2", layout=layout, resolver=_no_resolver) == 1


# --------------------------------------------------------------------------
# identity class change
# --------------------------------------------------------------------------

def test_identity_class_change_aborts_before_any_write(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _identity(layout, "dev-silo", "tool", expected_package="tool")

    def changed(image_ref, selector):
        return {"present": True, "resolved_path": "/usr/bin/tool",
                "executable_digest": GEN_B, "package": "evil-fork",
                "is_wrapper": False}

    rc = promote.promote("dev-silo", "run-a", layout=layout, resolver=changed)
    assert rc == 1
    # fail closed: nothing written
    assert not os.path.isfile(layout.binding_file("dev-silo"))
    assert not os.path.isdir(layout.generations_dir("tier2-dev"))
    assert not os.path.isdir(os.path.join(layout.pins_dir, "tier2-dev"))


def test_identity_same_package_proceeds(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _identity(layout, "dev-silo", "tool", expected_package="tool")

    def same(image_ref, selector):
        return {"present": True, "resolved_path": "/usr/bin/tool",
                "executable_digest": GEN_A, "package": "tool", "is_wrapper": False}

    rc = promote.promote("dev-silo", "run-a", layout=layout, resolver=same)
    assert rc == 0
    binding = qt.read_binding(layout.binding_file("dev-silo"))
    assert binding["active_generation"] == GEN_A
    # identity resolution recorded as generation evidence
    assert os.path.isfile(os.path.join(
        layout.generation_dir("tier2-dev", GEN_A), "evidence", "identity-tool.toml"))


def test_identity_wrapper_change_aborts(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _identity(layout, "dev-silo", "tool", expected_package="tool")

    def wrapper(image_ref, selector):
        return {"present": True, "resolved_path": "/opt/evil/tool",
                "executable_digest": GEN_A, "package": "tool",
                "is_wrapper": True, "selinux_type": "qdistro_tool_t"}

    assert promote.promote("dev-silo", "run-a", layout=layout, resolver=wrapper) == 1
    assert not os.path.isfile(layout.binding_file("dev-silo"))


def test_identity_selinux_type_change_aborts(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _validated_candidate(layout, "tier2-dev", "run-b", GEN_B)
    _identity(layout, "dev-silo", "tool", expected_package="tool")

    def same_type(image_ref, selector):
        return {"present": True, "resolved_path": "/usr/bin/tool",
                "executable_digest": GEN_A, "package": "tool",
                "is_wrapper": False, "selinux_type": "qdistro_tool_t"}

    # First promote records selinux_type=qdistro_tool_t for GEN_A.
    assert promote.promote("dev-silo", "run-a", layout=layout, resolver=same_type) == 0
    # Now the selector's declared type changes; promoting B must abort.
    idir = layout.identity_for("dev-silo")
    data = qt.read_toml(os.path.join(idir, "tool.toml"))
    data["identity"]["executable"]["selinux_type"] = "qdistro_other_t"
    qt.write_toml_atomic(os.path.join(idir, "tool.toml"), data, 0o644)

    def new_type(image_ref, selector):
        return {"present": True, "resolved_path": "/usr/bin/tool",
                "executable_digest": GEN_B, "package": "tool",
                "is_wrapper": False, "selinux_type": "qdistro_other_t"}

    assert promote.promote("dev-silo", "run-b", layout=layout, resolver=new_type) == 1
    # binding still points at A (unflipped)
    assert qt.read_binding(layout.binding_file("dev-silo"))["active_generation"] == GEN_A


def test_selector_class_change_detection():
    selector = {"executable": {"path_in_template": "/usr/bin/tool",
                               "expected_package": "tool"}}
    absent = {"present": False, "package": "", "resolved_path": "", "is_wrapper": False}
    assert promote.selector_class_change(selector, absent) is not None
    other_pkg = {"present": True, "package": "other", "resolved_path": "/x", "is_wrapper": False}
    assert promote.selector_class_change(selector, other_pkg) is not None
    same = {"present": True, "package": "tool", "resolved_path": "/x", "is_wrapper": False}
    assert promote.selector_class_change(selector, same) is None
    # wrapper without opt-in is a class change; with opt-in it is allowed
    wrap = {"present": True, "package": "tool", "resolved_path": "/x", "is_wrapper": True}
    assert promote.selector_class_change(selector, wrap) is not None
    sel_allow = {"executable": dict(selector["executable"], allow_wrapper=True)}
    assert promote.selector_class_change(sel_allow, wrap) is None
    # SELinux type change vs prior record
    sel_t = {"executable": dict(selector["executable"], selinux_type="t2")}
    res_t = {"present": True, "package": "tool", "resolved_path": "/x",
             "is_wrapper": False, "selinux_type": "t2"}
    assert promote.selector_class_change(sel_t, res_t, prior={"selinux_type": "t1"}) is not None
    assert promote.selector_class_change(sel_t, res_t, prior={"selinux_type": "t2"}) is None


def test_crash_second_promote_keeps_outgoing_active_pin(tmp_path, monkeypatch):
    # Pre-commit pin writes are additive only: if we crash before the
    # binding commit on a SECOND promote, the still-active outgoing
    # generation must retain its active pin (GC must never collect it).
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _validated_candidate(layout, "tier2-dev", "run-b", GEN_B)
    assert promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver) == 0

    def boom(path, binding):
        raise RuntimeError("kill -9 before binding commit")

    monkeypatch.setattr(qt, "write_binding", boom)
    with pytest.raises(RuntimeError):
        promote.promote("dev-silo", "run-b", layout=layout, resolver=_no_resolver)
    # binding unchanged (still A); outgoing A keeps its active pin AND gains a
    # rollback-window pin; both are unexpired so GC protects A.
    assert qt.read_binding(layout.binding_file("dev-silo"))["active_generation"] == GEN_A
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_A), "active.toml"))
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_A), "rollback-window.toml"))


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------

def test_rollback_flips_back(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _validated_candidate(layout, "tier2-dev", "run-b", GEN_B)
    promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver)
    promote.promote("dev-silo", "run-b", layout=layout, resolver=_no_resolver)
    # now active=B, previous=[A]; roll back to A
    rc = promote.promote("dev-silo", rollback=GEN_A, layout=layout,
                         resolver=_no_resolver, image_exists=lambda d: True)
    assert rc == 0
    binding = qt.read_binding(layout.binding_file("dev-silo"))
    assert binding["active_generation"] == GEN_A
    assert binding["previous_generations"] == [GEN_B]
    assert binding["identity_revision"] == 3
    # both generations pinned during the window
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_A), "active.toml"))
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_B), "rollback-window.toml"))


def test_rollback_refused_when_payload_collected(tmp_path):
    # The generation record can outlive the image payload (evidence outlives
    # payload). Rollback to a digest with no image must be refused, not flip
    # the binding to an unlaunchable target.
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    _validated_candidate(layout, "tier2-dev", "run-b", GEN_B)
    promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver,
                    image_exists=lambda d: True)
    promote.promote("dev-silo", "run-b", layout=layout, resolver=_no_resolver,
                    image_exists=lambda d: True)
    # A's record exists but its image was GC'd -> rollback refused.
    rc = promote.promote("dev-silo", rollback=GEN_A, layout=layout,
                         resolver=_no_resolver, image_exists=lambda d: d != GEN_A)
    assert rc == 1
    assert qt.read_binding(layout.binding_file("dev-silo"))["active_generation"] == GEN_B


def test_rollback_unknown_target_refused(tmp_path):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)
    promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver)
    assert promote.promote("dev-silo", rollback=GEN_B, layout=layout) == 1


def test_rollback_no_binding_refused(tmp_path):
    layout = _layout(tmp_path)
    assert promote.promote("dev-silo", rollback=GEN_A, layout=layout) == 1


# --------------------------------------------------------------------------
# crash consistency
# --------------------------------------------------------------------------

def test_crash_between_pins_and_binding_leaves_old_binding(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _validated_candidate(layout, "tier2-dev", "run-a", GEN_A)

    real_write_binding = qt.write_binding

    def boom(path, binding):
        raise RuntimeError("kill -9 between pin-write and binding-write")

    monkeypatch.setattr(qt, "write_binding", boom)
    with pytest.raises(RuntimeError):
        promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver)
    # binding never created (silo stays untemplated); extra pins are harmless
    assert not os.path.isfile(layout.binding_file("dev-silo"))
    assert os.path.isfile(os.path.join(layout.pins_for("tier2-dev", GEN_A), "active.toml"))

    # recovery: re-run promote succeeds and produces a consistent binding
    monkeypatch.setattr(qt, "write_binding", real_write_binding)
    assert promote.promote("dev-silo", "run-a", layout=layout, resolver=_no_resolver) == 0
    binding = qt.read_binding(layout.binding_file("dev-silo"))
    assert binding["active_generation"] == GEN_A
