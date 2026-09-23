"""Tests for the third batch of built-in plugins:
trash, open_with, git_status, folder_size, mount_manager,
embedded_viewer, rclone, sync_folders.

Same rules as the earlier suites — pure-logic surfaces only, no
subprocess calls and no Qt dialogs run. The Qt widget classes are
exercised at import time and through plugin discovery; their dialog
methods are not invoked because they require user interaction.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from qfileman.plugin import MenuProvider, PluginManager
from qfileman.plugins.builtin import embedded_viewer as ev_mod
from qfileman.plugins.builtin import folder_size as fs_mod
from qfileman.plugins.builtin import git_status as gs_mod
from qfileman.plugins.builtin import mount_manager as mm_mod
from qfileman.plugins.builtin import open_with as ow_mod
from qfileman.plugins.builtin import rclone as rc_mod
from qfileman.plugins.builtin import sync_folders as sf_mod
from qfileman.plugins.builtin import trash as tr_mod

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "trash", "open_with", "git_status", "folder_size",
    "mount_manager", "embedded_viewer", "rclone", "sync_folders",
])
def test_new_plugins_discovered(name):
    pm = PluginManager()
    pm.discover()
    assert name in pm.available_plugins()


@pytest.mark.parametrize("name", [
    "trash", "open_with", "git_status", "folder_size",
    "mount_manager", "embedded_viewer", "rclone", "sync_folders",
])
def test_new_plugins_load(name):
    pm = PluginManager()
    pm.discover()
    plugin = pm.load(name)
    assert plugin is not None
    assert isinstance(plugin, MenuProvider)


# ---------------------------------------------------------------------------
# trash
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="trash_argv passes the target as a list element after a `--` "
    "end-of-options separator, so a path is never word-split or "
    "interpreted as a flag",
    severity="high",
    cheats=[
        "drop the `--` from the expected argv",
        "relax the exact-list `==` to a membership/substring check",
    ],
    consequence="a path that starts with `-` (e.g. `--force`) or contains "
    "spaces could be parsed as options/extra args — argument injection "
    "into the trash backend",
)
def test_trash_argv_gio(monkeypatch):
    monkeypatch.setattr(tr_mod, "choose_backend",
                        lambda: ("gio", tr_mod._BACKENDS[0][1]))
    argv = tr_mod.trash_argv("/tmp/foo bar")
    assert argv == ["gio", "trash", "--", "/tmp/foo bar"]


def test_trash_argv_trash_put(monkeypatch):
    monkeypatch.setattr(tr_mod, "choose_backend",
                        lambda: ("trash-put", tr_mod._BACKENDS[1][1]))
    argv = tr_mod.trash_argv("/tmp/foo")
    assert argv == ["trash-put", "--", "/tmp/foo"]


def test_trash_argv_none_when_no_backend(monkeypatch):
    monkeypatch.setattr(tr_mod, "choose_backend", lambda: None)
    assert tr_mod.trash_argv("/tmp/foo") is None


def test_trash_menu_hidden_when_no_backend(monkeypatch):
    monkeypatch.setattr(tr_mod, "choose_backend", lambda: None)
    plugin = tr_mod.TrashPlugin()
    assert plugin.get_menu_items("/tmp/foo") == []


def test_trash_menu_visible_when_backend_present(monkeypatch):
    monkeypatch.setattr(tr_mod, "choose_backend",
                        lambda: ("gio", tr_mod._BACKENDS[0][1]))
    plugin = tr_mod.TrashPlugin()
    labels = [lbl for lbl, _cb in plugin.get_menu_items("/tmp/foo")]
    assert labels == ["Move to Trash"]


# ---------------------------------------------------------------------------
# open_with
# ---------------------------------------------------------------------------

def test_format_exec_substitutes_f():
    argv = ow_mod.format_exec("kate %f", "/home/me/a.txt")
    # The path may be quoted but must round-trip cleanly via shlex.
    import shlex
    assert argv[0] == "kate"
    assert shlex.join(argv) == "kate /home/me/a.txt" or argv[1] == "/home/me/a.txt"


def test_format_exec_substitutes_u_as_uri():
    argv = ow_mod.format_exec("firefox %u", "/home/me/page.html")
    assert argv[0] == "firefox"
    assert any(a.startswith("file://") for a in argv)


def test_format_exec_quotes_paths_with_spaces():
    argv = ow_mod.format_exec("vlc %f", "/home/me/movie file.mkv")
    # The path must arrive as a single argv element, intact.
    assert "/home/me/movie file.mkv" in argv


def test_format_exec_drops_field_codes_we_dont_care_about():
    argv = ow_mod.format_exec("foo %i %c %k %f", "/a")
    assert argv[0] == "foo"
    # No empty noise tokens.
    assert "" not in argv


def test_format_exec_appends_path_if_template_omits_it():
    # A handler whose Exec has no %f at all should still receive the file.
    argv = ow_mod.format_exec("less", "/etc/hostname")
    assert argv == ["less", "/etc/hostname"]


def test_parse_desktop_entry(tmp_path):
    f = tmp_path / "x.desktop"
    f.write_text(textwrap.dedent("""\
        [Desktop Entry]
        Name=Example
        Exec=example %f
        # comment line
        NoDisplay=false

        [Desktop Action New]
        Name=NewWindow
        Exec=example --new
    """))
    entry = ow_mod._parse_desktop_entry(str(f))
    assert entry is not None
    assert entry["Name"] == "Example"
    assert entry["Exec"] == "example %f"
    # Action section keys must NOT bleed into the main section.
    assert entry["Exec"] != "example --new"


def test_handlers_for_mime_reads_mimeinfo_cache(tmp_path, monkeypatch):
    apps = tmp_path / "data" / "applications"
    apps.mkdir(parents=True)
    (apps / "myviewer.desktop").write_text(textwrap.dedent("""\
        [Desktop Entry]
        Name=My Viewer
        Exec=myviewer %f
    """))
    (apps / "mimeinfo.cache").write_text(
        "[MIME Cache]\n"
        "text/plain=myviewer.desktop;\n"
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_DIRS", "")
    handlers = ow_mod.handlers_for("text/plain")
    assert any(h[1] == "myviewer.desktop" for h in handlers)
    name, _id, exec_line = next(h for h in handlers if h[1] == "myviewer.desktop")
    assert name == "My Viewer"
    assert exec_line == "myviewer %f"


# ---------------------------------------------------------------------------
# git_status
# ---------------------------------------------------------------------------

def test_find_repo_root_returns_dir_with_dotgit(tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert gs_mod.find_repo_root(str(sub)) == str(tmp_path)


def test_find_repo_root_handles_gitfile(tmp_path):
    """``.git`` as a file (worktrees, submodules) must also count."""
    (tmp_path / ".git").write_text("gitdir: ../other/.git/worktrees/me\n")
    assert gs_mod.find_repo_root(str(tmp_path)) == str(tmp_path)


def test_find_repo_root_none_outside_repo(tmp_path):
    assert gs_mod.find_repo_root(str(tmp_path)) is None


def test_git_menu_hidden_outside_repo(tmp_path, monkeypatch):
    # Pretend git is installed so the early-out doesn't fire.
    monkeypatch.setattr(gs_mod.shutil, "which", lambda _n: "/usr/bin/git")
    plugin = gs_mod.GitStatusPlugin()
    assert plugin.get_menu_items(str(tmp_path)) == []


def test_git_menu_visible_inside_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(gs_mod.shutil, "which", lambda _n: "/usr/bin/git")
    (tmp_path / ".git").mkdir()
    plugin = gs_mod.GitStatusPlugin()
    labels = [lbl for lbl, _cb in plugin.get_menu_items(str(tmp_path))]
    assert "Git: Status" in labels
    assert "Git: Diff" in labels
    assert "Git: Commit…" in labels
    # Directory → no Blame entry.
    assert not any("Blame" in lbl for lbl in labels)


def test_git_menu_blame_on_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gs_mod.shutil, "which", lambda _n: "/usr/bin/git")
    (tmp_path / ".git").mkdir()
    f = tmp_path / "a.txt"
    f.write_text("x")
    plugin = gs_mod.GitStatusPlugin()
    labels = [lbl for lbl, _cb in plugin.get_menu_items(str(f))]
    assert "Git: Blame" in labels


# ---------------------------------------------------------------------------
# folder_size
# ---------------------------------------------------------------------------

def test_directory_size_sums_files(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "b").write_bytes(b"y" * 200)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "c").write_bytes(b"z" * 50)
    assert fs_mod.directory_size(str(tmp_path)) == 350


def test_directory_size_handles_empty(tmp_path):
    assert fs_mod.directory_size(str(tmp_path)) == 0


@pytest.mark.cheat_aware(
    protects="directory_size uses lstat and does NOT follow symlinks, so a "
    "size walk cannot be lured off-tree by a planted symlink",
    severity="high",
    cheats=[
        "widen the `total < 1500` bound to absorb a followed-link size",
        "remove the symlink from the fixture so the case is never exercised",
    ],
    consequence="a symlink-following size walk can be steered to traverse "
    "and inflate over arbitrary targets outside the directory",
)
def test_directory_size_does_not_follow_symlinks(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"x" * 1000)
    link = tmp_path / "link"
    link.symlink_to(big)
    # The target counts only once (via 'big'); the symlink contributes
    # only the lstat size of the link entry itself, which is small.
    total = fs_mod.directory_size(str(tmp_path))
    assert 1000 <= total < 1500


def test_format_size_units():
    assert fs_mod.format_size(0) == "0 B"
    assert fs_mod.format_size(512) == "512 B"
    assert fs_mod.format_size(1024) == "1.0 KiB"
    assert fs_mod.format_size(1024 * 1024) == "1.0 MiB"
    assert fs_mod.format_size(1024 ** 3) == "1.0 GiB"


def test_child_sizes_returns_per_child(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.bin").write_bytes(b"y" * 100)
    rows = fs_mod.child_sizes(str(tmp_path))
    by_name = {name: (size, is_dir) for name, size, is_dir in rows}
    assert by_name["a.txt"] == (10, False)
    assert by_name["sub"] == (100, True)


# ---------------------------------------------------------------------------
# mount_manager
# ---------------------------------------------------------------------------

def test_parse_lsblk_json_flattens_children():
    payload = json.dumps({
        "blockdevices": [
            {
                "name": "sda",
                "size": "500G",
                "fstype": None,
                "mountpoint": None,
                "children": [
                    {"name": "sda1", "size": "100G", "fstype": "ext4",
                     "mountpoint": "/"},
                    {"name": "sda2", "size": "400G", "fstype": "ext4",
                     "mountpoint": None},
                ],
            },
        ],
    })
    rows = mm_mod.parse_lsblk_json(payload)
    names = [r["name"] for r in rows]
    assert names == ["sda1", "sda2"]
    assert rows[0]["mountpoint"] == "/"


def test_parse_lsblk_json_handles_mountpoints_array():
    """Newer util-linux emits ``mountpoints`` (array) instead of ``mountpoint``."""
    payload = json.dumps({
        "blockdevices": [
            {"name": "sdb1", "size": "10G", "fstype": "vfat",
             "mountpoints": ["/mnt/usb", None], "mountpoint": None},
        ],
    })
    rows = mm_mod.parse_lsblk_json(payload)
    assert rows[0]["mountpoint"] == "/mnt/usb"


def test_parse_lsblk_json_bad_input_returns_empty():
    assert mm_mod.parse_lsblk_json("not json") == []
    assert mm_mod.parse_lsblk_json("{}") == []


def test_udisksctl_argv_shape():
    assert mm_mod._udisksctl_argv("mount", "/dev/sda1") == \
        ["udisksctl", "mount", "-b", "/dev/sda1"]
    assert mm_mod._udisksctl_argv("unmount", "/dev/sda1")[1] == "unmount"


# ---------------------------------------------------------------------------
# embedded_viewer
# ---------------------------------------------------------------------------

def test_detect_kind_text():
    assert ev_mod.detect_kind("/tmp/x.py", sample=b"print('hi')\n") == "text"


def test_detect_kind_image_by_extension():
    # No sample needed — extension wins.
    assert ev_mod.detect_kind("/tmp/x.png") == "image"
    assert ev_mod.detect_kind("/tmp/x.JPEG") == "image"


def test_detect_kind_hex_on_nul_bytes():
    sample = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 16
    assert ev_mod.detect_kind("/tmp/binary", sample=sample) == "hex"


def test_format_hex_layout():
    out = ev_mod.format_hex(b"ABC123")
    assert out.startswith("00000000  ")
    # The single line must include both hex and ASCII columns.
    assert "41 42 43" in out
    assert "ABC123" in out


def test_format_hex_multirow():
    out = ev_mod.format_hex(b"\x00" * 20)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("00000000  ")
    assert lines[1].startswith("00000010  ")


def test_format_hex_respects_base_offset():
    out = ev_mod.format_hex(b"\xff", base_offset=0x1000)
    assert out.startswith("00001000")


# ---------------------------------------------------------------------------
# rclone
# ---------------------------------------------------------------------------

def test_rclone_argv_copy():
    argv = rc_mod.rclone_argv("copy", "/src", "myremote:dst")
    assert argv[0] == "rclone"
    assert argv[1] == "copy"
    assert "--progress" in argv
    assert argv[-2:] == ["/src", "myremote:dst"]
    assert "--dry-run" not in argv


def test_rclone_argv_sync():
    argv = rc_mod.rclone_argv("sync", "/src", "myremote:dst")
    assert argv[1] == "sync"


def test_rclone_argv_dry_run():
    argv = rc_mod.rclone_argv("copy", "/src", "r:dst", dry_run=True)
    assert "--dry-run" in argv


def test_rclone_argv_dash_path_after_terminator():
    # A leading-dash source must be a path, not an rclone flag: it has to
    # appear after the ``--`` terminator (argv-injection guard).
    argv = rc_mod.rclone_argv("copy", "--config=/evil", "r:dst")
    assert "--" in argv
    assert argv.index("--") < argv.index("--config=/evil")
    assert argv[-2:] == ["--config=/evil", "r:dst"]


def test_rclone_menu_hidden_when_no_binary(monkeypatch):
    monkeypatch.setattr(rc_mod.shutil, "which", lambda _n: None)
    assert rc_mod.RclonePlugin().get_menu_items("/tmp/foo") == []


def test_rclone_menu_visible_when_binary_present(monkeypatch):
    monkeypatch.setattr(rc_mod.shutil, "which", lambda _n: "/usr/bin/rclone")
    labels = [lbl for lbl, _cb in
              rc_mod.RclonePlugin().get_menu_items("/tmp/foo")]
    assert "Rclone Copy To…" in labels
    assert any("Sync" in label for label in labels)


# ---------------------------------------------------------------------------
# sync_folders
# ---------------------------------------------------------------------------

def test_dry_run_argv_adds_trailing_slash():
    argv = sf_mod.dry_run_argv("/a", "/b")
    assert argv[-2:] == ["/a/", "/b/"]
    assert "--dry-run" in argv
    assert "--itemize-changes" in argv
    assert "--delete" in argv


def test_apply_argv_has_resume_flags():
    argv = sf_mod.apply_argv("/a/", "/b/")
    for flag in ("--partial", "--append-verify", "--inplace", "--delete"):
        assert flag in argv


def test_describe_change_new_file():
    # >f+++++++++ → all-plus rest means a brand-new file.
    assert sf_mod.describe_change(">f++++++++") == "new"


def test_describe_change_update_file():
    # >f.st...... → existing file with size/time changed.
    assert sf_mod.describe_change(">f.st....") == "update"


def test_describe_change_delete():
    # rsync's --delete emits ``*deleting`` for removed files.
    assert sf_mod.describe_change("*deleting") == "delete"


def test_describe_change_create_dir():
    assert sf_mod.describe_change("cd+++++++++") == "create"


def test_parse_itemize_picks_change_lines_only():
    sample = (
        ">f+++++++++ new.txt\n"
        ">f.st...... updated.txt\n"
        "*deleting   old.txt\n"
        "\n"
        "sent 1234 bytes  received 567 bytes  100.00 bytes/sec\n"
        "total size is 0  speedup is 1.00\n"
    )
    rows = sf_mod.parse_itemize(sample)
    actions = [a for a, _p in rows]
    paths = [p for _a, p in rows]
    assert "new" in actions
    assert "update" in actions
    assert "delete" in actions
    # Summary lines must not appear.
    assert not any("sent" in p for p in paths)
    assert not any("total" in p for p in paths)
