"""Source guard for the decisive R9 production window-position path."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "multimachine/harness/drive-r9-rdp-output.py"
LAUNCHER = ROOT / "multimachine/harness/vm/r9-rdp-external-launch.py"


def test_r9_build_and_launcher_do_not_enable_test_placement() -> None:
    driver = DRIVER.read_text(encoding="utf-8")
    launcher = LAUNCHER.read_text(encoding="utf-8")

    assert "-Denable_test_place=true" not in driver
    assert "QDWIN_TEST_PLACE_APPID" not in launcher
    assert "QDWIN_TEST_PLACE_X" not in launcher
    assert "QDWIN_TEST_PLACE_Y" not in launcher


def test_r9_positions_both_epochs_through_bound_qdshell() -> None:
    driver = DRIVER.read_text(encoding="utf-8")

    assert "def position_marker_through_shell(" in driver
    assert "call qdwin positionWindow" in driver
    assert driver.count("position_marker_through_shell(") == 3
    assert "generation_90_shell_position" in driver
    assert "generation_91_shell_position" in driver
    assert "production_qdwin_has_no_test_placement_path" in driver
    assert '"(clamped)" in line' in driver
