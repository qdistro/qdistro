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


def _pass_runner(image_ref, probe, ctr, evidence_dir=None):
    return {"name": probe["name"], "kind": probe["kind"], "class": "local-runtime",
            "required": bool(probe.get("required", True)), "result": "pass",
            "duration_seconds": 0.1, "reason": "", "artifacts": []}


def _fail_runner_for(target):
    def runner(image_ref, probe, ctr, evidence_dir=None):
        r = _pass_runner(image_ref, probe, ctr, evidence_dir)
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


# --------------------------------------------------------------------------
# page-open: screenshot validation + evidence plumbing (fableplan2 task 03)
# --------------------------------------------------------------------------

import struct
import zlib


def _png(width, height, rows, *, colortype=2, filtertype=0):
    """Minimal PNG encoder for tests (no PIL). `rows` is a list of raw
    scanline bytes (already the right length for colortype); every scanline
    uses `filtertype`."""
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colortype, 0, 0, 0)
    raw = b"".join(bytes([filtertype]) + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _solid_rgb(width, height, rgb):
    row = bytes(rgb) * width
    return _png(width, height, [row] * height)


def test_png_uniform_frame_fails():
    data = _solid_rgb(16, 16, (29, 78, 216))  # solid blue
    ok, why = validate._png_is_nonuniform(data)
    assert ok is False
    assert "uniform" in why


def test_png_nonuniform_frame_passes():
    # 16x16 blue with one yellow pixel in the middle row.
    blue = bytes((29, 78, 216))
    rows = [blue * 16 for _ in range(16)]
    midrow = bytearray(blue * 16)
    midrow[8 * 3:8 * 3 + 3] = bytes((253, 224, 71))  # one yellow pixel
    rows[8] = bytes(midrow)
    data = _png(16, 16, rows)
    ok, why = validate._png_is_nonuniform(data)
    assert ok is True


def test_png_nonuniform_with_up_filter():
    # Exercise the unfilter path: row 0 solid, row 1 a different colour using
    # filter type 2 (Up) so the stored bytes are deltas, not the raw colour.
    blue = bytes((10, 20, 30))
    green = bytes((10, 200, 30))
    row0 = blue * 8
    # Up filter: stored = current - above. Make row1 actually green.
    delta = bytes([(green[i % 3] - blue[i % 3]) & 0xFF for i in range(8 * 3)])
    data = _png(8, 2, [row0, delta], filtertype=0)  # row0 filter 0
    # Re-encode with row1 using filter 2: build manually.
    def chunk(typ, d):
        return (struct.pack(">I", len(d)) + typ + d
                + struct.pack(">I", zlib.crc32(typ + d) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 8, 2, 8, 2, 0, 0, 0)
    raw = b"\x00" + row0 + b"\x02" + delta
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    ok, why = validate._png_is_nonuniform(data)
    assert ok is True


def test_screenshot_verdict_missing_and_invalid(tmp_path):
    missing = str(tmp_path / "nope.png")
    ok, why = validate.screenshot_verdict(missing)
    assert ok is False and "not produced" in why
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"this is not a png at all, just some long filler bytes...")
    ok, why = validate.screenshot_verdict(str(bad))
    assert ok is False and "PNG" in why


def test_screenshot_verdict_accepts_real_render(tmp_path):
    blue = bytes((29, 78, 216))
    rows = [blue * 16 for _ in range(16)]
    rows[8] = bytes(bytearray(blue * 16)[:24] + bytes((253, 224, 71)) + blue * 7)
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png(16, 16, rows))
    ok, why = validate.screenshot_verdict(str(shot))
    assert ok is True


def test_chromium_headless_args_are_pinned():
    # The slice reuses one arg set (codex r5): --no-sandbox is mandatory
    # (the container is the sandbox), and the fixed viewport is recorded.
    args = validate.CHROMIUM_HEADLESS_ARGS
    assert "--no-sandbox" in args
    assert "--window-size=1024,768" in args
    assert validate.PAGE_OPEN_VIEWPORT == "1024x768"
    # The page-open command writes the local page and screenshots it.
    script = validate.page_open_script()
    assert "QDISTRO-PAGE-OPEN-OK" in script
    assert "--screenshot=" in script and "file:///tmp/probe.html" in script


def test_page_open_evidence_referenced_in_report(tmp_path):
    # The runner is injected; it simulates page-open by writing a screenshot
    # into the per-check evidence dir and returning its artifact. The report
    # must reference the artifact and the file must exist on disk.
    layout, run_id, cdir = _built_candidate(tmp_path, probes=[
        {"name": "process-starts", "kind": "process", "command": "true"},
        {"name": "page-open", "kind": "page-open", "required": True, "timeout": 120},
    ])

    def gui_runner(image_ref, probe, ctr, evidence_dir):
        if probe["kind"] == "page-open":
            dst = os.path.join(evidence_dir, f"page-open-{probe['name']}.png")
            with open(dst, "wb") as fh:
                fh.write(_solid_rgb(8, 8, (1, 2, 3)))
            return {"name": probe["name"], "kind": "page-open",
                    "class": "local-runtime", "required": True, "result": "pass",
                    "duration_seconds": 1.0, "reason": "",
                    "artifacts": [os.path.basename(dst)], "viewport": "1024x768"}
        return _pass_runner(image_ref, probe, ctr, evidence_dir)

    rc = validate.validate(run_id, layout=layout, runner=gui_runner)
    assert rc == 0
    report = qt.read_toml(os.path.join(cdir, "evidence", "validation.toml"))
    po = [c for c in report["check"] if c["name"] == "page-open"][0]
    assert po["artifacts"] == ["page-open-page-open.png"]
    assert po["viewport"] == "1024x768"
    assert os.path.isfile(os.path.join(cdir, "evidence", "page-open-page-open.png"))


def test_window_probe_fails_closed(tmp_path):
    # window is optional/stretch and must keep failing closed (not a gate):
    # run_probe raises, and validate()'s loop turns that into a failed check.
    with pytest.raises(qt.TemplateError, match="window"):
        validate.run_probe("sha256:" + "b" * 64,
                            {"name": "win", "kind": "window"}, "ctr", None)
    layout, run_id, cdir = _built_candidate(tmp_path, probes=[
        {"name": "win", "kind": "window", "required": True},
    ])
    # No podman is invoked: the window raise happens before any container.
    rc = validate.validate(run_id, layout=layout)
    assert rc == 1
    report = qt.read_toml(os.path.join(cdir, "evidence", "validation.toml"))
    assert report["check"][0]["result"] == "fail"


def _nonuniform_png(w=64, h=64):
    blue = bytes((29, 78, 216))
    rows = [blue * w for _ in range(h)]
    mid = bytearray(blue * w)
    mid[0:3] = bytes((253, 224, 71))  # one yellow pixel
    rows[h // 2] = bytes(mid)
    return _png(w, h, rows)


def test_run_gui_probe_enforces_nonuniform_screenshot(tmp_path, monkeypatch):
    # Non-vacuous: drive the REAL _run_gui_probe with a monkeypatched podman
    # that writes a screenshot into the bind scratch dir, and assert the
    # verdict tracks screenshot_verdict — a uniform (blank/failed) render
    # FAILS, a non-uniform render PASSES and is referenced as evidence.
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    png = {"data": _solid_rgb(64, 64, (255, 255, 255))}  # uniform white = blank

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["podman", "run"]:
            shotdir = None
            for i, c in enumerate(cmd):
                if c == "-v" and i + 1 < len(cmd) and "/shots" in cmd[i + 1]:
                    shotdir = cmd[i + 1].split(":", 1)[0]
            assert shotdir and os.path.isdir(shotdir)
            with open(os.path.join(shotdir, "page-open.png"), "wb") as fh:
                fh.write(png["data"])
        return P()  # also covers the `podman rm -f` cleanup call

    monkeypatch.setattr(validate.subprocess, "run", fake_run)
    probe = {"name": "page-open", "kind": "page-open", "required": True,
             "timeout": 120}

    res = validate._run_gui_probe("sha256:" + "b" * 64, probe, "ctr", str(evidence))
    assert res["result"] == "fail", "a uniform/blank render must NOT pass the gate"
    assert "uniform" in res["reason"]

    png["data"] = _nonuniform_png()
    res = validate._run_gui_probe("sha256:" + "b" * 64, probe, "ctr", str(evidence))
    assert res["result"] == "pass"
    assert res["artifacts"] == ["page-open-page-open.png"]
    assert res["viewport"] == "1024x768"
    assert os.path.isfile(evidence / "page-open-page-open.png")


def test_run_gui_probe_rejects_symlink_screenshot(tmp_path, monkeypatch):
    # A broken/malicious chromium that replaces the screenshot with a symlink
    # must not make the validator read off-scratch (O_NOFOLLOW + S_ISREG).
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(_nonuniform_png())

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["podman", "run"]:
            for i, c in enumerate(cmd):
                if c == "-v" and i + 1 < len(cmd) and "/shots" in cmd[i + 1]:
                    shotdir = cmd[i + 1].split(":", 1)[0]
                    os.symlink(str(secret), os.path.join(shotdir, "page-open.png"))
        return P()

    monkeypatch.setattr(validate.subprocess, "run", fake_run)
    probe = {"name": "page-open", "kind": "page-open", "required": True}
    res = validate._run_gui_probe("sha256:" + "b" * 64, probe, "ctr", str(evidence))
    assert res["result"] == "fail"
    assert not os.path.exists(evidence / "page-open-page-open.png")
