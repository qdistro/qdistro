"""Unit tests for qdistro-template-gc (todo/fableplan task 07).

GC is security-critical: it must never collect a pinned generation, must
fail closed on a corrupt receipt, and must delete only payloads — never
evidence, manifests, or audit records."""
from __future__ import annotations

import os

import pytest

import qdistro_templates as qt
import qdistro_template_gc as gc


def _layout(tmp_path):
    layout = qt.Layout(etc=str(tmp_path / "etc"), var=str(tmp_path / "var"))
    qt.ensure_skeleton(layout)
    return layout


def _gen(layout, template, gen, *, mtime=None):
    gen_dir = layout.generation_dir(template, gen)
    ev = os.path.join(gen_dir, "evidence")
    os.makedirs(ev, exist_ok=True)
    manifest = {"template": template, "run_id": gen[:12], "image_digest": gen,
                "image_id": gen, "containerfile_digest": gen, "build_command": "x",
                "network_mode": "unrestricted", "artifact_manifest": [],
                "generation_ref": gen}
    qt.write_toml_atomic(os.path.join(gen_dir, "manifest.toml"), manifest, 0o644)
    qt.write_toml_atomic(os.path.join(ev, "validation.toml"), {"result": "validated"}, 0o644)
    if mtime is not None:
        os.utime(gen_dir, (mtime, mtime))
    return gen_dir


def _pin(layout, template, gen, reason, *, expires_at=None, owner="dev-silo"):
    pin = {"owner_type": "silo", "owner_id": owner, "reason": reason,
           "generation": gen, "template": template}
    if expires_at is not None:
        pin["expires_at"] = expires_at
    path = os.path.join(layout.pins_for(template, gen), f"{reason}.toml")
    qt.write_pin(path, pin)


def _binding(layout, silo, template, active):
    binding = {"silo": silo, "template": template, "backend": "podman-image",
               "active_generation": active, "previous_generations": [],
               "state_path": f"/v/{silo}", "activation_policy": "manual",
               "identity_revision": 1}
    qt.write_binding(layout.binding_file(silo), binding)


def _digest(n):
    return "sha256:" + str(n) * 64


def _collected_runner():
    deleted = []
    def rmi(ref):
        deleted.append(ref)
        return True
    return deleted, rmi


def _exists_true(_ref):
    return True


# The cascade-guard tag/untag are real-podman side effects (like rmi); inject
# no-ops so the retention unit tests stay hermetic. A dedicated test below
# verifies the guard wiring with recording fakes.
def _noop_tag(digest):
    # Return a truthy dummy tag: the cascade guard treats a None return as a
    # tag FAILURE (and fail-closes), so a hermetic no-op must signal success.
    return f"noop:{digest.split(':')[-1]}"


def _noop_untag(_tag):
    pass


def _status_present(_digest):
    # Cascade-guard tri-state existence probe forced to "present" so the guard
    # exercises its tagging path (the real _image_status shells out to podman).
    return "present"


def _candidate(layout, template, run_id, *, mtime, state="failed", gen=None):
    """A failed candidate dir. With gen=None it is a failed-BUILD candidate
    (no manifest, no image); with a digest it gets a valid candidate manifest
    whose generation_ref is that digest."""
    cdir = layout.candidate_dir(template, run_id)
    os.makedirs(cdir, exist_ok=True)
    if gen is not None:
        ev = os.path.join(cdir, "evidence")
        os.makedirs(ev, exist_ok=True)
        manifest = {"template": template, "run_id": run_id, "image_digest": gen,
                    "image_id": gen, "containerfile_digest": gen,
                    "build_command": "x", "network_mode": "unrestricted",
                    "artifact_manifest": [], "generation_ref": gen}
        qt.write_toml_atomic(os.path.join(cdir, "manifest.toml"), manifest, 0o644)
        qt.write_toml_atomic(os.path.join(ev, "validation.toml"),
                             {"result": "failed"}, 0o644)
    qt.set_candidate_state(cdir, state)
    os.utime(cdir, (mtime, mtime))
    return cdir


def test_pinned_generation_survives_aggressive_retention(tmp_path):
    layout = _layout(tmp_path)
    # retention keeps 1 promoted generation
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=1), 0o644)
    now = 1_700_000_000.0
    a, b, c = _digest(1), _digest(2), _digest(3)
    _gen(layout, "tier2-dev", a, mtime=now - 300)   # oldest
    _gen(layout, "tier2-dev", b, mtime=now - 200)
    _gen(layout, "tier2-dev", c, mtime=now - 100)   # newest -> kept by retention
    # pin the OLDEST with an unexpired rollback-window
    _pin(layout, "tier2-dev", a, "rollback-window", expires_at="2099-01-01T00:00:00Z")
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, tag=_noop_tag, untag=_noop_untag)
    # c kept by retention (newest), a kept by pin, b collected
    assert b in deleted
    assert a not in deleted and c not in deleted


def test_expired_rollback_pin_frees_generation(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=1), 0o644)
    now = 1_700_000_000.0
    a, b = _digest(1), _digest(2)
    _gen(layout, "tier2-dev", a, mtime=now - 200)
    _gen(layout, "tier2-dev", b, mtime=now - 100)   # newest kept
    # a's rollback-window pin is EXPIRED -> a is collectable
    _pin(layout, "tier2-dev", a, "rollback-window", expires_at="2000-01-01T00:00:00Z")
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, tag=_noop_tag, untag=_noop_untag)
    assert a in deleted


def test_binding_active_generation_never_collected(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    _binding(layout, "dev-silo", "tier2-dev", a)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, tag=_noop_tag, untag=_noop_untag)
    assert a not in deleted, "the live launch target must never be collected"


def test_stale_active_pin_does_not_protect(tmp_path):
    # An `active` pin whose generation is NOT a binding's active (left by a
    # mid-promote crash) must not protect the generation.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    _pin(layout, "tier2-dev", a, "active")  # stale: no binding references a
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, tag=_noop_tag, untag=_noop_untag)
    assert a in deleted


def test_corrupt_pin_aborts_run(tmp_path):
    layout = _layout(tmp_path)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    # A parseable but incomplete pin receipt (missing required keys) must
    # abort the whole run — a corrupt pin must never read as "unpinned".
    pdir = os.path.join(layout.pins_dir, "tier2-dev", a)
    os.makedirs(pdir)
    qt.atomic_write(os.path.join(pdir, "rollback-window.toml"),
                    'owner_type = "silo"\n', 0o600)
    deleted, rmi = _collected_runner()
    with pytest.raises(qt.TemplateError):
        gc.build_pinned_set(layout, now)
    assert deleted == []


def test_unparseable_pin_aborts_via_main(tmp_path, monkeypatch):
    # Invalid TOML in a receipt aborts main() with exit 2 (fail closed),
    # deleting nothing.
    layout = _layout(tmp_path)
    monkeypatch.setenv("QDISTRO_ETC_DIR", str(tmp_path / "etc"))
    monkeypatch.setenv("QDISTRO_VAR_DIR", str(tmp_path / "var"))
    a = _digest(1)
    _gen(layout, "tier2-dev", a)
    pdir = os.path.join(layout.pins_dir, "tier2-dev", a)
    os.makedirs(pdir)
    qt.atomic_write(os.path.join(pdir, "rollback-window.toml"),
                    "not = valid pin\n", 0o600)
    assert gc.main([]) == 2


def test_corrupt_expiry_aborts(tmp_path):
    layout = _layout(tmp_path)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    _pin(layout, "tier2-dev", a, "rollback-window", expires_at="not-a-date")
    with pytest.raises(ValueError):
        gc.build_pinned_set(layout, now)


def test_evidence_survives_payload_deletion(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    a = _digest(1)
    gen_dir = _gen(layout, "tier2-dev", a, mtime=now - 100)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, tag=_noop_tag, untag=_noop_untag)
    assert a in deleted
    # manifest + evidence still on disk
    assert os.path.isfile(os.path.join(gen_dir, "manifest.toml"))
    assert os.path.isfile(os.path.join(gen_dir, "evidence", "validation.toml"))


def test_failed_candidate_payload_collected_after_window(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    payload = _digest(7)
    cdir = _candidate(layout, "tier2-dev", "run-old", mtime=now - 8 * 86400,
                      gen=payload)  # older than 7d, with a real payload digest
    deleted, rmi = _collected_runner()
    res = gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    assert any(d["kind"] == "candidate" and d["run_id"] == "run-old" for d in res)
    # the payload is reclaimed by the manifest's generation_ref digest, NOT a
    # per-run candidate tag (which the build untags immediately).
    assert payload in deleted
    assert not any(ref.startswith("qdistro-candidate/") for ref in deleted)
    # evidence (the candidate dir + its manifest) still present
    assert os.path.isdir(cdir)
    assert os.path.isfile(os.path.join(cdir, "manifest.toml"))
    # a gc.deleted row was written naming the digest
    db = os.path.join(layout.var, "audit", "template_audit.sqlite")
    import qdistro_template_audit as audit
    alog = audit.TemplateAuditLog(db)
    try:
        rows = [r for r in alog.recent() if r["event"] == "template.gc.deleted"]
        assert any(r["run_id"] == "run-old" and r["generation"] == payload
                   for r in rows)
    finally:
        alog.close()


def test_recent_failed_candidate_kept(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    _candidate(layout, "tier2-dev", "run-new", mtime=now - 1 * 86400,
               gen=_digest(7))  # 1 day old
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    assert deleted == []


def test_failed_build_candidate_no_payload_no_rmi(tmp_path):
    # A failed BUILD leaves no manifest and no image: "nothing to collect",
    # not an error — no rmi, no exception, run continues.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    _candidate(layout, "tier2-dev", "run-nobuild", mtime=now - 8 * 86400, gen=None)
    deleted, rmi = _collected_runner()
    res = gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    assert deleted == []
    assert not any(d["kind"] == "candidate" for d in res)


def test_failed_candidate_digest_pinned_under_other_template_not_collected(tmp_path):
    # A failed candidate under template X whose payload digest is pinned as a
    # live generation of ANOTHER template Y (a cache-identical rebuild shares
    # the image_id) must NOT be rmi'd — the generation path owns that payload.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    shared = _digest(5)
    # live generation + active binding for template "tier2-other"
    _gen(layout, "tier2-other", shared, mtime=now - 100)
    _binding(layout, "other-silo", "tier2-other", shared)
    # failed candidate under tier2-dev with the same payload digest
    _candidate(layout, "tier2-dev", "run-shared", mtime=now - 8 * 86400, gen=shared)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    assert shared not in deleted


def test_failed_candidate_digest_with_promoted_record_not_collected(tmp_path):
    # A failed candidate whose digest has a promoted generation record (even
    # unpinned, even under another template) must NOT be rmi'd: the generation
    # retention path owns its payload.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=3, failed_candidate_days=7),
                         0o644)
    now = 1_700_000_000.0
    shared = _digest(6)
    _gen(layout, "tier2-other", shared, mtime=now - 100)  # generation record exists
    _candidate(layout, "tier2-dev", "run-shared2", mtime=now - 8 * 86400, gen=shared)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    assert shared not in deleted


def test_failed_candidate_absent_image_silent_no_audit(tmp_path):
    # A manifest whose image is already gone (rmi'd by an earlier run) is
    # already-collected: no rmi WARN, no audit row, no exception.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    _candidate(layout, "tier2-dev", "run-gone", mtime=now - 8 * 86400, gen=_digest(8))
    deleted, rmi = _collected_runner()
    res = gc.gc(layout=layout, now=now, rmi=rmi, image_exists=lambda _r: False)
    assert deleted == []
    assert res and res[0]["deleted"] is False
    db = os.path.join(layout.var, "audit", "template_audit.sqlite")
    if os.path.isfile(db):
        import qdistro_template_audit as audit
        alog = audit.TemplateAuditLog(db)
        try:
            assert [r for r in alog.recent()
                    if r["event"] == "template.gc.deleted"] == []
        finally:
            alog.close()


def test_failed_candidate_corrupt_manifest_skipped_run_continues(tmp_path):
    # A corrupt/untrusted manifest in a failed candidate must NOT abort the
    # run nor read as collectable: skip it, continue with the rest.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    bad = layout.candidate_dir("tier2-dev", "run-corrupt")
    os.makedirs(bad)
    qt.atomic_write(os.path.join(bad, "manifest.toml"),
                    'template = "tier2-dev"\n', 0o644)  # missing required keys
    qt.set_candidate_state(bad, "failed")
    os.utime(bad, (now - 8 * 86400, now - 8 * 86400))
    good_payload = _digest(9)
    _candidate(layout, "tier2-dev", "run-good", mtime=now - 8 * 86400,
               gen=good_payload)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    # corrupt one skipped, good one still collected
    assert good_payload in deleted


def test_failed_candidate_unparseable_manifest_skipped(tmp_path):
    # Syntactically-invalid TOML (tomllib raises a ValueError subclass) in a
    # failed candidate manifest must also be skipped, not abort the run.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file,
                         dict(gc.DEFAULT_RETENTION, failed_candidate_days=7), 0o644)
    now = 1_700_000_000.0
    bad = layout.candidate_dir("tier2-dev", "run-badtoml")
    os.makedirs(bad)
    qt.atomic_write(os.path.join(bad, "manifest.toml"), "this is not = valid\n", 0o644)
    qt.set_candidate_state(bad, "failed")
    os.utime(bad, (now - 8 * 86400, now - 8 * 86400))
    deleted, rmi = _collected_runner()
    res = gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true, tag=_noop_tag, untag=_noop_untag)
    assert deleted == []
    assert not any(d.get("run_id") == "run-badtoml" for d in res)


def test_dry_run_deletes_nothing(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    deleted, rmi = _collected_runner()
    res = gc.gc(layout=layout, now=now, dry_run=True, rmi=rmi)
    assert deleted == []  # rmi never called
    assert res and res[0]["deleted"] is False


def test_corrupt_retention_aborts(tmp_path):
    layout = _layout(tmp_path)
    qt.atomic_write(layout.retention_file, "keep_promoted_generations = true\n", 0o644)
    with pytest.raises(qt.TemplateError):
        gc.load_retention(layout)


def test_misplaced_pin_receipt_aborts(tmp_path):
    # A valid receipt filed under the wrong generation must abort (else the
    # generation it actually names could read as unpinned).
    layout = _layout(tmp_path)
    now = 1_700_000_000.0
    a, b = _digest(1), _digest(2)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    # receipt names generation b but is filed under a's pin dir
    pdir = os.path.join(layout.pins_dir, "tier2-dev", a)
    os.makedirs(pdir)
    qt.write_pin(os.path.join(pdir, "rollback-window.toml"),
                 {"owner_type": "silo", "owner_id": "s", "reason": "rollback-window",
                  "generation": b, "template": "tier2-dev",
                  "expires_at": "2099-01-01T00:00:00Z"})
    with pytest.raises(qt.TemplateError, match="filed under"):
        gc.build_pinned_set(layout, now)


def test_malformed_override_aborts_before_any_deletion(tmp_path):
    layout = _layout(tmp_path)
    # negative override count must be rejected up front
    qt.atomic_write(layout.retention_file,
                    "keep_promoted_generations = 3\n"
                    "keep_promoted_generations_vm = 2\n"
                    "failed_candidate_days = 7\n"
                    "build_log_days = 180\n"
                    "audit_evidence_years = 3\n"
                    "[overrides.tier2-dev]\n"
                    "keep_promoted_generations = -1\n", 0o644)
    with pytest.raises(qt.TemplateError):
        gc.load_retention(layout)


def test_rmi_failure_writes_no_audit_row(tmp_path):
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    res = gc.gc(layout=layout, now=now, rmi=lambda ref: False)  # rmi fails
    assert res[0]["deleted"] is False
    # no gc.deleted audit row written for a failed rmi
    import qdistro_template_audit as audit
    db = os.path.join(layout.var, "audit", "template_audit.sqlite")
    if os.path.isfile(db):
        alog = audit.TemplateAuditLog(db)
        try:
            assert [r for r in alog.recent() if r["event"] == "template.gc.deleted"] == []
        finally:
            alog.close()


def test_pinned_generation_image_is_tag_protected_during_sweep(tmp_path):
    # Cascade guard: generation images are stored UNTAGGED, and `podman rmi`
    # of a child (a failed candidate built FROM a pinned generation) would
    # cascade-delete the untagged parent. GC must tag every pinned generation
    # image with existing payload for the duration of the rmi sweep and untag
    # it afterwards. Verify the wiring with recording fakes.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    pinned_gen, victim = _digest(1), _digest(2)
    _gen(layout, "tier2-dev", pinned_gen, mtime=now - 200)
    _binding(layout, "dev-silo", "tier2-dev", pinned_gen)  # pinned (active)
    # a failed candidate built FROM the pinned generation, expired -> collected
    _candidate(layout, "tier2-dev", "20990101T000000Z-childcand",
               mtime=now - 10_000_000, state="failed", gen=victim)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0, failed_candidate_days=0), 0o644)

    tagged, untagged = [], []
    def tag(d):
        tagged.append(d); return f"qdistro-gc-protect:{d.split(':')[-1]}"
    def untag(t):
        untagged.append(t)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true,
          tag=tag, untag=untag, image_status=_status_present)
    # the pinned (active) generation image was protected for the sweep...
    assert pinned_gen in tagged, "pinned generation image must be tag-protected"
    # ...and the protective tag was removed afterwards (no permanent tag leak).
    assert untagged == [f"qdistro-gc-protect:{pinned_gen.split(':')[-1]}"]
    # the candidate child payload was still collected.
    assert victim in deleted


def test_cascade_guard_skipped_in_dry_run(tmp_path):
    # dry-run deletes nothing, so it must not mutate tags either.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    a = _digest(1)
    _gen(layout, "tier2-dev", a, mtime=now - 100)
    _binding(layout, "dev-silo", "tier2-dev", a)
    tagged = []
    gc.gc(layout=layout, now=now, dry_run=True, rmi=lambda r: True,
          image_exists=_exists_true, tag=lambda d: tagged.append(d) or "x",
          untag=lambda t: None)
    assert tagged == [], "dry-run must not tag/untag anything"


def test_cascade_guard_fails_closed_when_tag_fails(tmp_path):
    # If a pinned generation image cannot be tag-protected, GC must ABORT
    # before any rmi rather than expose it to a cascading delete.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0, failed_candidate_days=0), 0o644)
    now = 1_700_000_000.0
    pinned_gen, victim = _digest(1), _digest(2)
    _gen(layout, "tier2-dev", pinned_gen, mtime=now - 200)
    _binding(layout, "dev-silo", "tier2-dev", pinned_gen)
    _candidate(layout, "tier2-dev", "20990101T000000Z-childcand",
               mtime=now - 10_000_000, state="failed", gen=victim)
    deleted, rmi = _collected_runner()
    with pytest.raises(qt.TemplateError):
        gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true,
              tag=lambda d: None, untag=_noop_untag,  # tag always fails
              image_status=_status_present)
    assert deleted == [], "no rmi may run once a pinned image cannot be protected"


def test_cascade_guard_fails_closed_on_image_status_error(tmp_path):
    # If a kept image's existence is INDETERMINATE (podman errored, not a clean
    # absent), GC must abort before any rmi rather than leave it unprotected.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0, failed_candidate_days=0), 0o644)
    now = 1_700_000_000.0
    pinned_gen, victim = _digest(1), _digest(2)
    _gen(layout, "tier2-dev", pinned_gen, mtime=now - 200)
    _binding(layout, "dev-silo", "tier2-dev", pinned_gen)
    _candidate(layout, "tier2-dev", "20990101T000000Z-childcand",
               mtime=now - 10_000_000, state="failed", gen=victim)
    deleted, rmi = _collected_runner()
    tagged = []
    with pytest.raises(qt.TemplateError):
        gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true,
              tag=lambda d: tagged.append(d) or f"t:{d}", untag=_noop_untag,
              image_status=lambda _d: "error")
    assert deleted == [], "no rmi may run on an indeterminate kept-image probe"
    assert tagged == [], "must not even attempt to tag once status is error"


def test_keep_n_retention_survivor_is_tag_protected(tmp_path):
    # SHOULD: the guard must protect keep-N retention survivors too, not only
    # pinned digests — a kept-by-count generation is equally exposed to a
    # cascading rmi of a child built FROM it.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=1, failed_candidate_days=0), 0o644)
    now = 1_700_000_000.0
    old_gen, kept_gen = _digest(1), _digest(2)
    _gen(layout, "tier2-dev", old_gen, mtime=now - 300)    # beyond keep-1
    _gen(layout, "tier2-dev", kept_gen, mtime=now - 100)   # newest -> kept by count
    # no binding/pin: kept_gen survives ONLY by retention count, not a pin.
    deleted, rmi = _collected_runner()
    tagged = []
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true,
          tag=lambda d: tagged.append(d) or f"t:{d}", untag=_noop_untag,
          image_status=_status_present)
    assert kept_gen in tagged, "keep-N survivor must be cascade-protected"
    assert old_gen in deleted and kept_gen not in deleted


def test_kept_digest_not_collected_cross_template(tmp_path):
    # SHOULD: a digest kept under template T1 (pinned/active) but ALSO present as
    # a beyond-retention generation record under T2 must NOT be rmi'd by T2's
    # loop — a direct `podman rmi <digest>` would delete the image out from
    # under T1's pin. Mirrors the candidate-path digest guard.
    layout = _layout(tmp_path)
    qt.write_toml_atomic(layout.retention_file, dict(gc.DEFAULT_RETENTION,
                         keep_promoted_generations=0), 0o644)
    now = 1_700_000_000.0
    shared, t2_only = _digest(1), _digest(2)
    # T1 keeps `shared` (its active binding generation).
    _gen(layout, "tier2-a", shared, mtime=now - 100)
    _binding(layout, "silo-a", "tier2-a", shared)
    # T2 has the SAME digest as a beyond-retention record, plus an unrelated one.
    _gen(layout, "tier2-b", shared, mtime=now - 300)
    _gen(layout, "tier2-b", t2_only, mtime=now - 200)
    deleted, rmi = _collected_runner()
    gc.gc(layout=layout, now=now, rmi=rmi, image_exists=_exists_true,
          tag=_noop_tag, untag=_noop_untag, image_status=_status_present)
    assert shared not in deleted, "a digest kept under another template must not be rmi'd"
    assert t2_only in deleted, "a genuinely unpinned beyond-retention digest is still collected"
