"""Unit tests for fableplan2 task 05 — first-activation state snapshot.

Covers the orchestration (qdistro_state_snapshot), the snapshot-then-marker
ordering at the launch anchor (qdistro_resolve_binding), the strict-vs-
availability policy + break-glass waiver, the pre-migration-snapshot pin,
rollback argument validation, and snapshot retention in template-gc.

Host-testable: the copy mechanism is real (cp -a); the subvolume/snapper/btrfs
mechanism is exercised in the VM.
"""
from __future__ import annotations

import os
import time

import qdistro_templates as qt
import qdistro_state_snapshot as ss
import qdistro_resolve_binding as rb
import qdistro_template_promote as promote
import qdistro_template_gc as gc

import pytest


GEN_A = "sha256:" + "a" * 64
GEN_B = "sha256:" + "b" * 64
TEMPLATE = "tier2-browser"


def _layout(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    return layout


def _generation(layout, gen):
    gen_dir = layout.generation_dir(TEMPLATE, gen)
    os.makedirs(gen_dir, exist_ok=True)
    qt.write_toml_atomic(os.path.join(gen_dir, "manifest.toml"), {
        "template": TEMPLATE, "run_id": "r", "image_digest": gen,
        "image_id": gen, "containerfile_digest": gen,
        "build_command": "x", "network_mode": "unrestricted",
        "artifact_manifest": [], "generation_ref": gen,
    }, 0o644)


def _state(layout, silo, payload="A-PROFILE"):
    sp = layout.default_state_path(silo)
    os.makedirs(sp, mode=0o700, exist_ok=True)
    with open(os.path.join(sp, "cookies"), "w", encoding="utf-8") as fh:
        fh.write(payload)
    return sp


def _policy(layout, template, activation_snapshot):
    os.makedirs(layout.templates_etc, exist_ok=True)
    qt.write_toml_atomic(layout.template_policy(template), {
        "template": {"class": "derived",
                     "state_boundary": {"enforced": "partial"},
                     "activation_snapshot": activation_snapshot},
    }, 0o644)


def _binding(layout, silo, active, prev=None):
    qt.write_binding(layout.binding_file(silo), {
        "silo": silo, "template": TEMPLATE, "backend": "podman-image",
        "active_generation": active, "previous_generations": prev or [],
        "state_path": layout.default_state_path(silo),
        "activation_policy": "manual", "identity_revision": 1,
    })


# --------------------------------------------------------------------------
# orchestration: take_pre_activation_snapshot
# --------------------------------------------------------------------------

def test_snapshot_taken_writes_payload_meta_and_pin(tmp_path):
    layout = _layout(tmp_path)
    silo = "gmail"
    _state(layout, silo, "A-PROFILE")
    result = ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path(silo),
        policy="availability", now=1000.0)
    assert result["taken"] is True
    # payload captured the A-era profile
    with open(os.path.join(result["path"], "cookies")) as fh:
        assert fh.read() == "A-PROFILE"
    # meta records the generations + mechanism
    snaps = ss.list_snapshots(layout, silo)
    assert len(snaps) == 1
    assert snaps[0]["outgoing_generation"] == GEN_A
    assert snaps[0]["incoming_generation"] == GEN_B
    assert snaps[0]["mechanism"] == "copy"
    # pre-migration-snapshot pin written on the OUTGOING generation
    pin_path = os.path.join(layout.pins_for(TEMPLATE, GEN_A),
                            "pre-migration-snapshot.toml")
    assert os.path.isfile(pin_path)
    pin = qt.read_toml(pin_path)
    assert pin["reason"] == "pre-migration-snapshot"
    assert pin["generation"] == GEN_A


def test_no_snapshot_on_first_ever_activation(tmp_path):
    layout = _layout(tmp_path)
    _state(layout, "gmail")
    result = ss.take_pre_activation_snapshot(
        layout, "gmail", incoming_generation=GEN_A, outgoing_generation=None,
        template=TEMPLATE, state_path=layout.default_state_path("gmail"),
        policy="strict", now=1.0)
    assert result["taken"] is False
    assert ss.list_snapshots(layout, "gmail") == []


def test_strict_policy_snapshot_failure_raises(tmp_path):
    layout = _layout(tmp_path)
    # No state dir → snapshot of a missing state fails.
    with pytest.raises(ss.StrictSnapshotRefused):
        ss.take_pre_activation_snapshot(
            layout, "gmail", incoming_generation=GEN_B,
            outgoing_generation=GEN_A, template=TEMPLATE,
            state_path=layout.default_state_path("gmail"),
            policy="strict", now=1.0)


def test_availability_policy_records_unavailable_not_raise(tmp_path):
    layout = _layout(tmp_path)
    result = ss.take_pre_activation_snapshot(
        layout, "gmail", incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path("gmail"),
        policy="availability", now=1.0)
    assert result["taken"] is False
    assert result["unavailable"] is True


def test_waiver_is_one_shot_and_skips_snapshot(tmp_path):
    layout = _layout(tmp_path)
    _state(layout, "gmail")
    ss.write_waiver(layout, "gmail", GEN_B, "storage full, break glass", now=1.0)
    result = ss.take_pre_activation_snapshot(
        layout, "gmail", incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path("gmail"),
        policy="strict", now=2.0)
    assert result["waived"] is True
    assert ss.list_snapshots(layout, "gmail") == []   # no snapshot taken
    # waiver consumed: a second strict launch with no waiver now refuses.
    with pytest.raises(ss.StrictSnapshotRefused):
        # make the snapshot fail (remove state) to prove the waiver is gone
        import shutil
        shutil.rmtree(layout.default_state_path("gmail"))
        ss.take_pre_activation_snapshot(
            layout, "gmail", incoming_generation=GEN_B,
            outgoing_generation=GEN_A, template=TEMPLATE,
            state_path=layout.default_state_path("gmail"),
            policy="strict", now=3.0)


def test_waiver_requires_reason(tmp_path):
    layout = _layout(tmp_path)
    with pytest.raises(ss.StateSnapshotError):
        ss.write_waiver(layout, "gmail", GEN_B, "   ")


# --------------------------------------------------------------------------
# ordering at the launch anchor (resolve-binding --launch-env --record)
# --------------------------------------------------------------------------

def test_strict_failure_leaves_marker_uncommitted_and_retries(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    silo = "gmail"
    _policy(layout, TEMPLATE, "strict")
    _generation(layout, GEN_A)
    _generation(layout, GEN_B)
    # Mark A as already activated (so B is a first_activation transition), and
    # make the snapshot fail by pointing state_path at a non-dir.
    rb.record_activation(layout, silo, GEN_A, run_status_dir=str(tmp_path / "run"))
    _binding(layout, silo, active=GEN_B, prev=[GEN_A])
    # Remove the state dir so the strict snapshot fails.
    monkeypatch.setattr(rb, "RUN_STATUS_DIR", str(tmp_path / "run"))

    rc = rb._launch_env_main(silo, layout, record=True)
    assert rc == rb.RC_SNAPSHOT_REFUSED
    # marker still points at A (B's activation obligation NOT discharged)
    assert rb.read_activated_marker(layout, silo) == GEN_A

    # Now create the state so the retry's snapshot succeeds.
    _state(layout, silo, "A-PROFILE")
    rc2 = rb._launch_env_main(silo, layout, record=True)
    assert rc2 == 0
    assert rb.read_activated_marker(layout, silo) == GEN_B
    # the retry took the snapshot
    assert len(ss.list_snapshots(layout, silo)) == 1


def test_availability_flip_emits_single_activation_event(tmp_path, monkeypatch):
    """Regression (codex r1 SHOULD): an availability flip whose snapshot is
    unavailable must emit EXACTLY ONE template.binding.activated, carrying
    state_rollback=unavailable — not a plain second activation that hides it."""
    layout = _layout(tmp_path)
    silo = "gmail"
    _policy(layout, TEMPLATE, "availability")
    _generation(layout, GEN_A)
    _generation(layout, GEN_B)
    # No state dir → the availability snapshot is unavailable (not a refusal).
    rb.record_activation(layout, silo, GEN_A, run_status_dir=str(tmp_path / "run"))
    _binding(layout, silo, active=GEN_B, prev=[GEN_A])
    monkeypatch.setattr(rb, "RUN_STATUS_DIR", str(tmp_path / "run"))

    events = []
    real_emit = rb.audit.emit

    def capture(event, **fields):
        events.append((event, fields))
        return real_emit(event, **fields)

    monkeypatch.setattr(rb.audit, "emit", capture)
    rc = rb._launch_env_main(silo, layout, record=True)
    assert rc == 0
    activations = [f for (e, f) in events if e == "template.binding.activated"]
    assert len(activations) == 1
    assert activations[0].get("state_rollback") == "unavailable"


def test_successful_snapshot_then_marker_committed(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    silo = "gmail"
    _policy(layout, TEMPLATE, "strict")
    _generation(layout, GEN_A)
    _generation(layout, GEN_B)
    _state(layout, silo, "A-PROFILE")
    rb.record_activation(layout, silo, GEN_A, run_status_dir=str(tmp_path / "run"))
    _binding(layout, silo, active=GEN_B, prev=[GEN_A])
    monkeypatch.setattr(rb, "RUN_STATUS_DIR", str(tmp_path / "run"))
    rc = rb._launch_env_main(silo, layout, record=True)
    assert rc == 0
    # snapshot captured A's profile and marker advanced to B
    snaps = ss.list_snapshots(layout, silo)
    assert len(snaps) == 1 and snaps[0]["outgoing_generation"] == GEN_A
    assert rb.read_activated_marker(layout, silo) == GEN_B


# --------------------------------------------------------------------------
# rollback argument validation + restore
# --------------------------------------------------------------------------

def _promote_setup(tmp_path):
    layout = _layout(tmp_path)
    silo = "gmail"
    _generation(layout, GEN_A)
    _generation(layout, GEN_B)
    _state(layout, silo, "B-ERA")
    # Binding currently on B with A as a rollback target.
    _binding(layout, silo, active=GEN_B, prev=[GEN_A])
    return layout, silo


def test_rollback_requires_choice_when_snapshot_exists(tmp_path):
    layout, silo = _promote_setup(tmp_path)
    # A snapshot capturing A's state exists.
    ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path(silo),
        policy="availability", now=1.0)
    rc = promote.promote(silo, rollback=GEN_A, layout=layout,
                         image_exists=lambda d: True, running_check=lambda s: False)
    assert rc != 0   # refused: must choose --restore-state / --keep-state
    # binding unchanged (still B)
    assert qt.read_binding(layout.binding_file(silo))["active_generation"] == GEN_B


def test_rollback_keep_state_flips_binding_only(tmp_path):
    layout, silo = _promote_setup(tmp_path)
    ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path(silo),
        policy="availability", now=1.0)
    rc = promote.promote(silo, rollback=GEN_A, layout=layout, keep_state=True,
                         image_exists=lambda d: True, running_check=lambda s: False)
    assert rc == 0
    assert qt.read_binding(layout.binding_file(silo))["active_generation"] == GEN_A
    # state untouched (still B-era)
    with open(os.path.join(layout.default_state_path(silo), "cookies")) as fh:
        assert fh.read() == "B-ERA"


def test_rollback_restore_state_brings_back_old_state(tmp_path):
    layout, silo = _promote_setup(tmp_path)
    # Snapshot was taken when the live state was A-era; re-create that.
    sp = layout.default_state_path(silo)
    with open(os.path.join(sp, "cookies"), "w") as fh:
        fh.write("A-ERA")
    ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=sp, policy="availability", now=1.0)
    # Then B wrote new state.
    with open(os.path.join(sp, "cookies"), "w") as fh:
        fh.write("B-ERA")

    rc = promote.promote(silo, rollback=GEN_A, layout=layout, restore_state=True,
                         image_exists=lambda d: True, running_check=lambda s: False)
    assert rc == 0
    assert qt.read_binding(layout.binding_file(silo))["active_generation"] == GEN_A
    # state_path now holds the restored A-era profile
    with open(os.path.join(sp, "cookies")) as fh:
        assert fh.read() == "A-ERA"
    # the displaced B-era state is preserved aside, never deleted
    rejected = [d for d in os.listdir(layout.silo_dir(silo))
                if d.startswith("state-rejected-")]
    assert rejected
    with open(os.path.join(layout.silo_dir(silo), rejected[0], "cookies")) as fh:
        assert fh.read() == "B-ERA"


def test_rollback_restore_refused_when_flip_unsnapshotted(tmp_path):
    layout, silo = _promote_setup(tmp_path)
    # No snapshot for A → --restore-state must refuse and force --keep-state.
    rc = promote.promote(silo, rollback=GEN_A, layout=layout, restore_state=True,
                         image_exists=lambda d: True, running_check=lambda s: False)
    assert rc != 0
    assert qt.read_binding(layout.binding_file(silo))["active_generation"] == GEN_B


def test_rollback_refuses_when_silo_running(tmp_path):
    layout, silo = _promote_setup(tmp_path)
    sp = layout.default_state_path(silo)
    ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=sp, policy="availability", now=1.0)
    rc = promote.promote(silo, rollback=GEN_A, layout=layout, restore_state=True,
                         image_exists=lambda d: True, running_check=lambda s: True)
    assert rc != 0   # refuse: stop the silo first


def test_rollback_no_snapshot_defaults_keep_state(tmp_path):
    """A silo that never took a snapshot still rolls back (keep-state implicit)
    — this keeps the fableplan rollback path green."""
    layout, silo = _promote_setup(tmp_path)
    rc = promote.promote(silo, rollback=GEN_A, layout=layout,
                         image_exists=lambda d: True, running_check=lambda s: False)
    assert rc == 0
    assert qt.read_binding(layout.binding_file(silo))["active_generation"] == GEN_A


# --------------------------------------------------------------------------
# gc retention for state snapshots
# --------------------------------------------------------------------------

def test_gc_deletes_expired_snapshot_keeps_metadata(tmp_path):
    layout = _layout(tmp_path)
    silo = "gmail"
    _state(layout, silo, "A-PROFILE")
    r = ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path(silo),
        policy="availability", now=1000.0)
    snap_id = r["id"]
    payload = r["path"]
    assert os.path.isdir(payload)
    # Run gc well past the 14-day window.
    far_future = 1000.0 + (ss.SNAPSHOT_WINDOW_DAYS + 1) * 86400
    deletions = gc.gc(layout=layout, now=far_future,
                      rmi=lambda d: True, image_exists=lambda d: True)
    snap_dels = [d for d in deletions if d["kind"] == "state-snapshot"]
    assert len(snap_dels) == 1 and snap_dels[0]["deleted"] is True
    # payload gone, metadata kept, no longer a rollback choice
    assert not os.path.isdir(payload)
    meta = qt.read_toml(os.path.join(ss.snapshots_dir(layout, silo), snap_id,
                                     "meta.toml"))
    assert meta["restore_eligible"] == "false"
    assert "deleted_at" in meta
    assert ss.find_restore_snapshot(layout, silo, GEN_A) is None


def test_gc_keeps_unexpired_snapshot(tmp_path):
    layout = _layout(tmp_path)
    silo = "gmail"
    _state(layout, silo, "A-PROFILE")
    r = ss.take_pre_activation_snapshot(
        layout, silo, incoming_generation=GEN_B, outgoing_generation=GEN_A,
        template=TEMPLATE, state_path=layout.default_state_path(silo),
        policy="availability", now=1000.0)
    # Within the window → untouched, still a rollback target.
    deletions = gc.gc(layout=layout, now=1000.0 + 86400,
                      rmi=lambda d: True, image_exists=lambda d: True)
    assert [d for d in deletions if d["kind"] == "state-snapshot"] == []
    assert os.path.isdir(r["path"])
    assert ss.find_restore_snapshot(layout, silo, GEN_A) is not None
