"""The VM screenshot describer uses the sanctioned visual Codex driver."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.ui import runner


@pytest.mark.parametrize("with_image", [False, True])
def test_ui_codex_driver_pins_luna_and_keeps_rollout(monkeypatch, with_image):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        out = Path(cmd[cmd.index("--output-last-message") + 1])
        out.write_text("VERDICT: PASS\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    image = Path("/tmp/qdistro-ui-driver.png") if with_image else None
    assert runner._run_codex("describe this frame", image) == "VERDICT: PASS"

    cmd, kwargs = seen[0]
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--model") + 1] == "gpt-6-luna"
    assert cmd[cmd.index("-c") + 1] == "model_reasoning_effort=medium"
    assert "--ephemeral" not in cmd
    assert (f"--image={image}" in cmd) == with_image
    assert cmd[-1] == "describe this frame"
    assert kwargs["timeout"] == 180


@pytest.mark.parametrize(
    ("image_name", "expected_detail"),
    [
        ("bar_idle.png", "narrow strip pinned"),
        ("panel_calendar.png", "date whose cell"),
        ("panel_launcher.png", "visible caret"),
        ("settings_defaultapps-p2.png", "explanatory paragraph"),
        ("settings_bar_widgets.png", "Launcher widget itself"),
        ("settings_keyboard-p2.png", "cursor preference persistence"),
        ("settings_advanced.png", "warning card's color"),
        ("settings_windowmanager-p2.png", "dropdown arrow"),
        ("panel_battery.png", "only a header"),
        ("panel_wallpaper.png", "hidden-files eye"),
        ("panel_sessionmenu.png", "corner keybind badge"),
        ("panel_brightness.png", "icon before the slider"),
        ("panel_tray_degraded.png", "separate tray drawer surface"),
        ("settings_connections.png", "LEFT or RIGHT end of its track"),
        ("settings_connections-click1.png", "LEFT or RIGHT end of its track"),
        ("settings_desktopicons.png", "Checked and dimmed are independent"),
    ],
)
def test_ui_describer_details_are_scoped_to_surface(image_name, expected_detail):
    prompt = runner._describe_prompt_for(Path(image_name))
    assert expected_detail in prompt
    assert expected_detail not in runner._describe_prompt_for(
        Path("panel_network.png")
    )


@pytest.mark.parametrize(
    ("votes", "expected"),
    [
        (["PASS"], "PASS"),
        (["FAIL", "FAIL"], "FAIL"),
        (["FAIL", "PASS", "PASS"], "PASS"),
        (["FAIL", "PASS", "FAIL"], "FAIL"),
        (["FAIL", "SKIP", "PASS"], "FAIL"),
    ],
)
def test_visual_panel_confirmation_requires_independent_evidence(
        monkeypatch, tmp_path, votes, expected):
    seen = []
    frame = tmp_path / "panel.png"
    frame.write_bytes(b"saved frame")

    def fake_judge(_reference, description):
        vote = votes[len(seen)]
        seen.append((description, vote))
        missing = ["required content missing"] if vote == "FAIL" else []
        return runner.JudgeResult(vote, f"VERDICT: {vote}", missing, [])

    monkeypatch.setattr(runner, "judge", fake_judge)
    monkeypatch.setattr(runner, "describe", lambda _frame: "fresh observation")
    result = runner.judge_frame_consistently("required content", "initial", frame)

    assert result.verdict == expected
    assert [vote for _, vote in seen] == votes
    evidence = frame.with_suffix(".judge-recheck.txt")
    if len(votes) == 1:
        assert not evidence.exists()
    else:
        assert evidence.exists()
        assert evidence.read_text().count("Observation ") == len(votes)
        if expected == "FAIL":
            assert "required content missing" in result.missing


def test_describe_view_fails_loud_on_empty_with_backend(monkeypatch, tmp_path):
    frame = tmp_path / "settings_connections.png"
    monkeypatch.setattr(runner, "describe", lambda _frame: "  ")
    monkeypatch.setattr(runner, "_describe_backend_available", lambda: True)
    with pytest.raises(RuntimeError, match="settings_connections.png"):
        runner.describe_view(frame)


def test_describe_view_without_backend_keeps_judge_skip_path(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "describe", lambda _frame: "")
    monkeypatch.setattr(runner, "_describe_backend_available", lambda: False)
    assert runner.describe_view(tmp_path / "x.png") == ""


def test_capture_never_judges_a_subtab_view_whose_describe_was_empty(
        monkeypatch, tmp_path):
    """2026-10-13: the Wi-Fi frame's describe timed out twice and the empty
    text was stitched silently, so only the Bluetooth view was judged.
    Drive the real capture_surface_vm with the real connections surface."""
    from tests.ui.manifests import SETTINGS_SURFACES

    surface = next(s for s in SETTINGS_SURFACES if s.id == "settings_connections")
    assert surface.post_open_clicks, "connections must also capture Bluetooth"
    monkeypatch.setattr(runner, "ARTIFACTS_DIR", tmp_path)
    for name in ("ipc_vm", "mouse_wheel", "mouse_click", "mouse_move",
                 "tap_key"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "screenshot_vm",
                        lambda _s, out, **k: out.write_bytes(b"frame"))
    monkeypatch.setattr(runner, "_frames_identical", lambda *a, **k: True)
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner, "_describe_backend_available", lambda: True)
    monkeypatch.setattr(
        runner, "describe",
        lambda frame: "" if frame.name == "settings_connections.png"
        else "Bluetooth subtab: switch thumb left, dimmed")

    with pytest.raises(RuntimeError, match="settings_connections.png"):
        runner.capture_surface_vm(object(), surface)


# --- zoomed, contrast-stretched crops (settings_connections Bluetooth switch) ---

_CARD = (7, 7, 34)        # #070722, measured on the 2026-10-13 frame
_OUTLINE = (22, 22, 69)   # #161645, the disabled off-state track outline
_KNOB = (150, 150, 110)   # dimmed knob


def _connections_surface():
    from tests.ui.manifests import SETTINGS_SURFACES
    return next(s for s in SETTINGS_SURFACES if s.id == "settings_connections")


def _switch_frame(path, *, knob_left=True, size=(1280, 800)):
    """A 1280x800 frame shaped like the live Bluetooth subtab: a near-black
    card, a #161645 track outline at x 977-1020 / y 176-201 and a dimmed
    knob at its left (x 982-996) or right (x 1005-1019) end."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, _CARD)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((977, 176, 1020, 201), radius=12, outline=_OUTLINE,
                        width=1)
    kx = 982 if knob_left else 1001
    d.ellipse((kx, 182, kx + 14, 196), fill=_KNOB)
    img.save(path)
    return path


def _luma_columns(img):
    lum = img.convert("L")
    w, h = lum.size
    px = lum.load()
    return [sum(px[x, y] for y in range(h)) for x in range(w)]


def _track_mid_col(crop):
    x0 = runner._crop_box_px(crop.box, 1280, 800)[0]
    return int(((977 + 1020) / 2 - x0) * crop.scale)


def test_connections_surface_crops_the_bluetooth_switch_after_the_subtab_click():
    surface = _connections_surface()
    assert len(surface.zoom_crops) == 1
    crop = surface.zoom_crops[0]
    # view 1 = the frame after post_open_clicks[0], the Bluetooth subtab.
    assert crop.view == 1 and len(surface.post_open_clicks) >= 1
    x0, y0, x1, y1 = runner._crop_box_px(crop.box, 1280, 800)
    # The whole measured track (x 977-1020, y 176-201) lies inside the crop.
    assert x0 <= 977 and x1 >= 1021 and y0 <= 176 and y1 >= 202
    assert crop.scale >= 4


def test_zoom_crop_makes_a_dim_off_track_and_knob_position_observable(tmp_path):
    from PIL import Image
    crop = _connections_surface().zoom_crops[0]
    src = _switch_frame(tmp_path / "settings_connections-click1.png")
    out = runner.make_zoom_crop(src, tmp_path / "z.png", crop, {(1280, 800)})

    x0, y0, x1, y1 = runner._crop_box_px(crop.box, 1280, 800)
    raw_w, raw_h = (x1 - x0) * crop.scale, (y1 - y0) * crop.scale
    fields = Path(f"{out}.raw").read_text().split()
    assert fields[:3] == [str(raw_w), str(raw_h), "1"]
    assert fields[4] == f"crop:{x1 - x0}x{y1 - y0}+{x0}+{y0}"
    assert fields[5] == src.name
    with Image.open(out) as im:
        im.load()
        assert im.size == (raw_w + 1, raw_h + 1)        # unique-size padding
        raw = im.crop((0, 0, raw_w, raw_h))
    # Sidecar identity is the canonical raw-pixel digest (view-geometry I6).
    assert fields[3] == runner._view_pix_sha(raw)

    # The #161645 outline (luma ~25 on a ~9 card) is stretched to clearly
    # visible: sample the track's top edge in the middle of the track.
    lum = raw.convert("L")
    mid_x = (998 - x0) * crop.scale + crop.scale // 2
    top_y = (176 - y0) * crop.scale + crop.scale // 2
    with Image.open(src) as im:
        assert im.convert("L").getpixel((998, 176)) < 40   # dim at source
    assert lum.getpixel((mid_x, top_y)) > 150
    assert lum.getpixel((mid_x, top_y - 3 * crop.scale)) < 40   # card stays dark
    # The stretch is a per-channel non-decreasing remap, so luma order is
    # not guaranteed in general; for this near-grey knob on a near-black
    # card the knob mass still lands in the LEFT half of the track, so the
    # crop shows what the full frame did not. Position is MEASURED from the
    # unstretched frame (measure_switch_knob), not from this image.
    cols = _luma_columns(raw)
    mid = _track_mid_col(crop)
    assert sum(cols[:mid]) > sum(cols[mid:]) * 1.5


def test_zoom_crop_of_a_right_knob_is_not_a_left_knob(tmp_path):
    """The crop is evidence, not a constant: a switch drawn ON must yield a
    crop whose knob mass is on the right."""
    from PIL import Image
    crop = _connections_surface().zoom_crops[0]
    src = _switch_frame(tmp_path / "on.png", knob_left=False)
    out = runner.make_zoom_crop(src, tmp_path / "z.png", crop, set())
    raw_w, raw_h = map(int, Path(f"{out}.raw").read_text().split()[:2])
    with Image.open(out) as im:
        cols = _luma_columns(im.crop((0, 0, raw_w, raw_h)))
    mid = _track_mid_col(crop)
    assert sum(cols[mid:]) > sum(cols[:mid]) * 1.5


def test_zoom_crops_never_reuse_a_view_size_and_compare_by_raw_pixels(tmp_path):
    from PIL import Image
    crop = _connections_surface().zoom_crops[0]
    src = _switch_frame(tmp_path / "f.png")
    issued = {(1280, 800)}
    a = runner.make_zoom_crop(src, tmp_path / "a.png", crop, issued)
    b = runner.make_zoom_crop(src, tmp_path / "b.png", crop, issued)
    with Image.open(a) as ia, Image.open(b) as ib:
        assert ia.size != ib.size                 # never the same (W, H) twice
    sa, sb = (Path(f"{p}.raw").read_text().split() for p in (a, b))
    assert (sa[2], sb[2]) == ("1", "2")
    assert sa[3] == sb[3]                         # same raw pixels, by identity
    assert a.read_bytes() != b.read_bytes()       # file bytes are not identity


def test_zoom_crop_of_a_flat_region_fails_loud(tmp_path):
    from PIL import Image
    crop = _connections_surface().zoom_crops[0]
    src = tmp_path / "blank.png"
    Image.new("RGB", (1280, 800), _CARD).save(src)
    with pytest.raises(RuntimeError, match="flat color"):
        runner.make_zoom_crop(src, tmp_path / "z.png", crop, set())


def _drive_connections_capture(monkeypatch, tmp_path, describe,
                               bluetooth_frame=None):
    surface = _connections_surface()
    monkeypatch.setattr(runner, "ARTIFACTS_DIR", tmp_path)
    for name in ("ipc_vm", "mouse_wheel", "mouse_click", "mouse_move",
                 "tap_key"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)

    def fake_shot(_s, out, **_k):
        # The Wi-Fi view has its knob on the RIGHT, the Bluetooth view
        # (click1) on the LEFT: a crop of the wrong view is detectable.
        if out.name.endswith("-click1.png") and bluetooth_frame is not None:
            bluetooth_frame(out)
        else:
            _switch_frame(out, knob_left=out.name.endswith("-click1.png"))
        return out

    monkeypatch.setattr(runner, "screenshot_vm", fake_shot)
    monkeypatch.setattr(runner, "_frames_identical", lambda *a, **k: True)
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner, "_describe_backend_available", lambda: True)
    monkeypatch.setattr(runner, "describe", describe)
    return runner.capture_surface_vm(object(), surface)


def test_capture_describes_the_bluetooth_switch_crop_as_an_extra_view(
        monkeypatch, tmp_path):
    seen = []

    def describe(frame, prompt=None):
        seen.append((frame.name, prompt))
        if "-zoom" in frame.name:
            return "pill outline; round knob at the LEFT end; empty outline"
        return f"full view {frame.name}"

    _png, actual = _drive_connections_capture(monkeypatch, tmp_path, describe)

    names = [n for n, _ in seen]
    assert names[-1] == "settings_connections-zoom1.png"
    assert "settings_connections-click1.png" in names     # full views kept
    zoom_prompt = seen[-1][1]
    assert "MAGNIFIED" in zoom_prompt and "LEFT end" in zoom_prompt
    assert all(p is None for _, p in seen[:-1])           # full views unchanged
    assert "=== zoomed crop 1: Bluetooth master switch" in actual
    assert "round knob at the LEFT end" in actual
    assert "full view settings_connections-click1.png" in actual
    side = (tmp_path / "settings_connections-zoom1.png.raw").read_text().split()
    assert side[5] == "settings_connections-click1.png"   # cropped from view 1
    # Raw-pixel proof it came from the Bluetooth frame (knob LEFT), not the
    # Wi-Fi frame (knob RIGHT) cropped at the same box.
    crop = _connections_surface().zoom_crops[0]
    wifi = runner.make_zoom_crop(tmp_path / "settings_connections.png",
                                 tmp_path / "wifi-zoom.png", crop, set())
    assert Path(f"{wifi}.raw").read_text().split()[3] != side[3]


def test_capture_fails_loud_when_the_switch_crop_is_not_observed(
        monkeypatch, tmp_path):
    def describe(frame, prompt=None):
        return "" if "-zoom" in frame.name else "full view"

    with pytest.raises(RuntimeError, match="settings_connections-zoom1.png"):
        _drive_connections_capture(monkeypatch, tmp_path, describe)


def test_connections_expectation_requires_the_crop_to_show_a_left_knob():
    text = (runner.EXPECTATIONS_DIR / "settings_connections.md").read_text()
    assert "zoomed crop of the Bluetooth master switch" in text
    assert "knob at the LEFT end of that track" in text
    assert "A crop showing the knob at the RIGHT end, or no switch," in text


# --- the crop is authoritative for the Bluetooth knob; pixels enforce it ---

_FILL = (60, 60, 120)      # checked track fill (mPrimary, dimmed)
_DARK_KNOB = (14, 14, 40)  # checked knob (mOnPrimary), darker than the fill


def _checked_switch_frame(path):
    """The product's CHECKED NToggle shape, as measured on the real Wi-Fi
    switch of the 2026-10-14 frame (track x 977-1020, knob x ~998-1017):
    a FILLED track with a DARK knob at the right end. Its bright mass is
    left of centre, so a "where is the bright part" measurement would call
    this switch LEFT/off."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1280, 800), _CARD)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((977, 176, 1020, 201), radius=12, fill=_FILL,
                        outline=_OUTLINE, width=1)
    d.ellipse((1001, 182, 1015, 196), fill=_DARK_KNOB)
    img.save(path)
    return path


def _knobless_frame(path, knob_x=None):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1280, 800), _CARD)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((977, 176, 1020, 201), radius=12, outline=_OUTLINE)
    if knob_x is not None:
        d.ellipse((knob_x, 182, knob_x + 14, 196), fill=_KNOB)
    img.save(path)
    return path


def test_connections_crop_enforces_a_left_knob():
    assert _connections_surface().zoom_crops[0].knob_must_be == "left"


def test_measure_switch_knob_reads_left_and_right(tmp_path):
    crop = _connections_surface().zoom_crops[0]
    left = runner.measure_switch_knob(
        _switch_frame(tmp_path / "l.png", knob_left=True), crop)
    right = runner.measure_switch_knob(
        _switch_frame(tmp_path / "r.png", knob_left=False), crop)
    assert left["side"] == "left" and left["frac"] < 0.4, left
    assert left["track"] == (977, 1020)
    assert right["side"] == "right" and right["frac"] > 0.6, right


def test_measure_switch_knob_is_polarity_free_for_a_checked_switch(tmp_path):
    from PIL import Image
    crop = _connections_surface().zoom_crops[0]
    src = _checked_switch_frame(tmp_path / "checked.png")
    # The trap: the bright mass of a checked switch sits LEFT of centre.
    with Image.open(src) as im:
        cols = _luma_columns(im.crop((977, 176, 1021, 202)))
    mid = len(cols) // 2
    assert sum(cols[:mid]) > sum(cols[mid:])
    m = runner.measure_switch_knob(src, crop)
    assert m["side"] == "right", m


def test_measure_switch_knob_middle_and_blank_are_not_left(tmp_path):
    crop = _connections_surface().zoom_crops[0]
    mid = runner.measure_switch_knob(
        _knobless_frame(tmp_path / "mid.png", knob_x=992), crop)
    assert mid["side"] == "middle", mid
    empty = runner.measure_switch_knob(
        _knobless_frame(tmp_path / "empty.png"), crop)
    assert empty["side"] == "undetermined", empty


def test_full_frame_right_claim_does_not_override_a_left_crop(
        monkeypatch, tmp_path):
    """The 2026-10-14 conflict: the full-frame describer put the Bluetooth
    thumb RIGHT while the crop (and the pixels) show it LEFT. The capture
    keeps the full-frame text verbatim (no redaction), adds the measured
    LEFT line under the crop, and the golden tells the judge the crop is
    authoritative for that one knob, while keeping every failing branch."""
    full_claim = ("Visible switch row: Bluetooth. Its round thumb is at the "
                  "RIGHT end of an unfilled/dark track; the row is dimmed.")

    def describe(frame, prompt=None):
        if "-zoom" in frame.name:
            return "pill outline; the round knob sits at the LEFT end"
        if frame.name.endswith("-click1.png"):
            return full_claim
        return "Wi-Fi subtab"

    _png, actual = _drive_connections_capture(monkeypatch, tmp_path, describe)
    assert full_claim in actual
    crop_part = actual.split("=== zoomed crop 1:", 1)[1]
    assert "the round knob sits at the LEFT end" in crop_part
    assert "Harness pixel measurement of Bluetooth master switch" in crop_part
    assert "knob at the LEFT end" in crop_part

    text = (runner.EXPECTATIONS_DIR / "settings_connections.md").read_text()
    flat = " ".join(text.split())
    assert ("The crop is AUTHORITATIVE for the Bluetooth master switch's "
            "thumb position") in flat
    assert ("a full-frame view that places the Bluetooth thumb at the RIGHT "
            "end (or calls it on) is not evidence about its position") in flat
    assert ("must NOT be reported as MISSING, contradicted or EXTRA when the "
            "crop and the measurement both show the LEFT end") in flat
    # The exemption is scoped: everything else is still judged in full.
    assert ("This exemption covers only the Bluetooth master switch's thumb "
            "position") in flat
    assert ("The full-frame views must show this Bluetooth toggle row, its "
            "label and its dimmed/disabled state") in flat
    # The failing side of the bullet is intact.
    assert "A crop showing the knob at the RIGHT end, or no switch," in text
    assert "a measurement line that does not say LEFT" in flat


@pytest.mark.parametrize("frame", ["right_knob", "checked_product_shape"])
def test_a_genuinely_on_switch_fails_even_if_the_crop_is_described_left(
        monkeypatch, tmp_path, frame):
    """Authority for the crop must not become a pass for an ON switch: the
    pixels decide, so a describer that (wrongly) says LEFT cannot save it."""
    make = {
        "right_knob": lambda p: _switch_frame(p, knob_left=False),
        "checked_product_shape": _checked_switch_frame,
    }[frame]

    def describe(frame_path, prompt=None):
        if "-zoom" in frame_path.name:
            return "pill outline; the round knob sits at the LEFT end"
        return "Bluetooth thumb at the LEFT end"

    with pytest.raises(AssertionError) as exc:
        _drive_connections_capture(monkeypatch, tmp_path, describe,
                                   bluetooth_frame=make)
    msg = str(exc.value)
    assert "must have its knob at the LEFT end" in msg
    assert "the pixels say RIGHT" in msg
    assert "settings_connections-zoom1.png" in msg


def test_an_unmeasurable_switch_fails_closed(monkeypatch, tmp_path):
    def describe(frame_path, prompt=None):
        return "knob at the LEFT end"

    with pytest.raises(AssertionError, match="pixels say UNDETERMINED"):
        _drive_connections_capture(monkeypatch, tmp_path, describe,
                                   bluetooth_frame=_knobless_frame)


def test_zoom_crop_docstring_does_not_claim_luma_order_survives():
    doc = " ".join(runner.make_zoom_crop.__doc__.split())
    assert "brighter than which is preserved" not in doc
    assert "NOT guaranteed to survive" in doc


# --- panel_tray: the empty drawer auto-closes; the golden says so ---

def test_tray_golden_matches_the_empty_drawer_auto_close():
    qml = (runner.QDSHELL_ROOT / "Modules/Panels/Tray/TrayDrawerPanel.qml"
           ).read_text()
    flat_qml = " ".join(qml.split())
    # Product (8da5194c7, fixed by 8b97bfdb5): an already-empty drawer closes itself on open.
    assert ("panelContent.updateFilteredItems(); "
            "if (root.trayValues.length === 0) root.close();") in flat_qml
    text = " ".join((runner.EXPECTATIONS_DIR / "panel_tray.md").read_text()
                    .split())
    assert "An empty drawer is the right answer here" not in text
    assert "TrayDrawerPanel auto-closes when `trayValues` is empty" in text
    assert "do NOT require a visible empty drawer" in text
    # Still strict about what must NOT be on screen.
    for banned in ("no visible drawer surface",
                   "no empty or half-rendered drawer stub",
                   "no stale or placeholder tray icons",
                   "no other panel/surface", "no crash dialog"):
        assert banned in text
