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
    assert cmd[cmd.index("--model") + 1] == "gpt-5.6-luna"
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
