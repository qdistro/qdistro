"""Tests for CLI argument parsing."""

import pytest
from qterminator.__main__ import _execute_shell_command, parse_args

# --- Existing tests (preserved) ---


def test_default_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator"])
    args = parse_args()
    assert args.working_directory is None
    assert args.title is None
    assert args.execute is None
    assert args.geometry is None
    assert not args.no_restore


def test_working_directory(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-d", "/tmp"])
    args = parse_args()
    assert args.working_directory == "/tmp"


def test_title(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-T", "My Term"])
    args = parse_args()
    assert args.title == "My Term"


def test_geometry(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "1024x768"])
    args = parse_args()
    assert args.geometry == "1024x768"


def test_no_restore(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--no-restore"])
    args = parse_args()
    assert args.no_restore


def test_execute(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-x", "ls", "-la"])
    args = parse_args()
    assert args.execute == ["ls", "-la"]


def test_command_single(monkeypatch):
    """-e takes a single command string (Terminator-compatible)."""
    monkeypatch.setattr("sys.argv", ["qterminator", "-e", "htop"])
    args = parse_args()
    assert args.command == "htop"


# --- Default value tests ---


def test_default_no_restore_is_false(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator"])
    args = parse_args()
    assert args.no_restore is False


def test_default_execute_is_none(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator"])
    args = parse_args()
    assert args.execute is None


# --- Geometry: valid variations ---


def test_geometry_small(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "80x24"])
    args = parse_args()
    assert args.geometry == "80x24"


def test_geometry_large(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "3840x2160"])
    args = parse_args()
    assert args.geometry == "3840x2160"


# --- Geometry: invalid strings (parser accepts them; main() handles validation) ---


def test_geometry_missing_x(monkeypatch):
    """Parser accepts any string; splitting is done later in main()."""
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "1024"])
    args = parse_args()
    assert args.geometry == "1024"


def test_geometry_non_numeric(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "widexhigh"])
    args = parse_args()
    assert args.geometry == "widexhigh"


def test_geometry_empty_string(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", ""])
    args = parse_args()
    assert args.geometry == ""


def test_geometry_negative_values_rejected(monkeypatch):
    """Argparse treats -100x-200 as option flags, so this errors out."""
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "-100x-200"])
    with pytest.raises(SystemExit) as exc_info:
        parse_args()
    assert exc_info.value.code == 2


def test_geometry_zero_dimensions(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "0x0"])
    args = parse_args()
    assert args.geometry == "0x0"


# --- Execute variations ---


def test_execute_empty_list(monkeypatch):
    """With -x but no following args, REMAINDER gives an empty list."""
    monkeypatch.setattr("sys.argv", ["qterminator", "-x"])
    args = parse_args()
    assert args.execute == []


def test_execute_single_command(monkeypatch):
    """-x takes one or more args (Terminator-style multi-arg execute)."""
    monkeypatch.setattr("sys.argv", ["qterminator", "-x", "htop"])
    args = parse_args()
    assert args.execute == ["htop"]


def test_execute_multiple_args(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-x", "grep", "-rn", "foo", "."])
    args = parse_args()
    assert args.execute == ["grep", "-rn", "foo", "."]


def test_execute_shell_command_preserves_argv_metacharacters(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["qterminator", "-x", "printf", "%s\n", "a;touch /tmp/bad"],
    )
    args = parse_args()
    assert _execute_shell_command(args) == [
        "printf", "%s\n", "a;touch /tmp/bad",
    ]


def test_execute_shell_command_empty_execute_starts_normal_shell(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-x"])
    args = parse_args()
    assert _execute_shell_command(args) is None


# --- Working directory variations ---


def test_working_directory_long_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--working-directory", "/home/user"])
    args = parse_args()
    assert args.working_directory == "/home/user"


def test_working_directory_relative_path(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-d", "../relative/path"])
    args = parse_args()
    assert args.working_directory == "../relative/path"


def test_working_directory_with_spaces(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-d", "/path/with spaces/dir"])
    args = parse_args()
    assert args.working_directory == "/path/with spaces/dir"


# --- Title edge cases ---


def test_title_empty_string(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-T", ""])
    args = parse_args()
    assert args.title == ""


def test_title_special_characters(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-T", "Term <1> & \"quoted\""])
    args = parse_args()
    assert args.title == "Term <1> & \"quoted\""


def test_title_unicode(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-T", "Terminal \u2014 \u00e9\u00e8\u00ea"])
    args = parse_args()
    assert args.title == "Terminal \u2014 \u00e9\u00e8\u00ea"


# --- Combining multiple flags ---


def test_geometry_and_title(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "800x600", "-T", "Dev"])
    args = parse_args()
    assert args.geometry == "800x600"
    assert args.title == "Dev"


def test_all_flags_combined(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "qterminator",
        "-d", "/tmp",
        "-T", "Work",
        "--geometry", "1920x1080",
        "--no-restore",
        "-x", "bash",
    ])
    args = parse_args()
    assert args.working_directory == "/tmp"
    assert args.title == "Work"
    assert args.geometry == "1920x1080"
    assert args.no_restore is True
    assert args.execute == ["bash"]


def test_no_restore_with_working_directory(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--no-restore", "-d", "/var/log"])
    args = parse_args()
    assert args.no_restore is True
    assert args.working_directory == "/var/log"


# --- --version ---


def test_version_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--version"])
    with pytest.raises(SystemExit) as exc_info:
        parse_args()
    assert exc_info.value.code == 0


# --- Unknown arguments ---


def test_unknown_argument_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--bogus-flag"])
    with pytest.raises(SystemExit) as exc_info:
        parse_args()
    assert exc_info.value.code == 2


# --- New Terminator-compatible flags ---


def test_maximise_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-m"])
    args = parse_args()
    assert args.maximise is True


def test_maximise_long(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--maximize"])
    args = parse_args()
    assert args.maximise is True


def test_fullscreen_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-f"])
    args = parse_args()
    assert args.fullscreen is True


def test_borderless_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-b"])
    args = parse_args()
    assert args.borderless is True


def test_hidden_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-H"])
    args = parse_args()
    assert args.hidden is True


def test_profile_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-p", "myprofile"])
    args = parse_args()
    assert args.profile == "myprofile"


def test_layout_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-l", "work"])
    args = parse_args()
    assert args.layout == "work"


def test_role_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-r", "scratchpad"])
    args = parse_args()
    assert args.role == "scratchpad"


def test_icon_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "-i", "/path/icon.png"])
    args = parse_args()
    assert args.icon == "/path/icon.png"


def test_new_tab_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--new-tab"])
    args = parse_args()
    assert args.new_tab is True


def test_list_profiles_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--list-profiles"])
    args = parse_args()
    assert args.list_profiles is True


def test_list_layouts_flag(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--list-layouts"])
    args = parse_args()
    assert args.list_layouts is True


def test_geometry_with_position(monkeypatch):
    monkeypatch.setattr("sys.argv", ["qterminator", "--geometry", "1024x768+100+50"])
    args = parse_args()
    assert args.geometry == "1024x768+100+50"
