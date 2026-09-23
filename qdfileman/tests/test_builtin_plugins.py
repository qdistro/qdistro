"""Tests for the additional built-in plugins.

These tests stick to the pure-logic surface — argv builders, format
detection, hashing, and rename-template rendering — so they don't need
``rsync``, ``scp``, ``tar`` or any other external tool to be installed
on the box running the suite.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import zipfile
from unittest.mock import patch

import pytest
from qfileman.plugin import MenuProvider, PluginManager
from qfileman.plugins.builtin import archive as archive_mod
from qfileman.plugins.builtin import checksum as checksum_mod
from qfileman.plugins.builtin import multi_rename as mr_mod
from qfileman.plugins.builtin import remote_copy as rc_mod
from qfileman.plugins.builtin import rsync_sync as rs_mod

# ---------------------------------------------------------------------------
# Discovery: every new plugin must be findable by the manager.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "archive", "remote_copy", "rsync_sync", "checksum", "multi_rename",
])
def test_new_plugins_discovered(name):
    pm = PluginManager()
    pm.discover()
    assert name in pm.available_plugins()


@pytest.mark.parametrize("name", [
    "archive", "remote_copy", "rsync_sync", "checksum", "multi_rename",
])
def test_new_plugins_load(name):
    pm = PluginManager()
    pm.discover()
    plugin = pm.load(name)
    assert plugin is not None
    assert isinstance(plugin, MenuProvider)


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("foo.tar", "tar"),
    ("foo.tar.gz", "tar.gz"),
    ("foo.TGZ", "tar.gz"),
    ("foo.tar.bz2", "tar.bz2"),
    ("foo.tbz2", "tar.bz2"),
    ("foo.tar.xz", "tar.xz"),
    ("foo.txz", "tar.xz"),
    ("foo.tar.zst", "tar.zst"),
    ("foo.zip", "zip"),
    ("foo.7z", "7z"),
    ("foo.rar", "rar"),
    ("foo.gz", None),  # bare .gz is not an archive in this scheme
    ("plain.txt", None),
])
def test_detect_format(path, expected):
    assert archive_mod.detect_format(path) == expected


def test_extract_argv_tar():
    argv = archive_mod.extract_argv("/a/b.tar.gz", "/dst")
    assert argv == ["tar", "--keep-old-files", "-xf", "/a/b.tar.gz", "-C", "/dst"]


def test_extract_argv_zip():
    argv = archive_mod.extract_argv("/a/b.zip", "/dst")
    assert argv == ["unzip", "-n", "/a/b.zip", "-d", "/dst"]


def test_extract_argv_7z():
    argv = archive_mod.extract_argv("/a/b.7z", "/dst")
    assert argv == ["7z", "x", "-o/dst", "-aos", "/a/b.7z"]


def test_extract_argv_rar_does_not_overwrite():
    argv = archive_mod.extract_argv("/a/b.rar", "/dst")
    assert argv == ["unrar", "x", "-o-", "/a/b.rar", "/dst/"]


def test_extract_argv_unknown():
    assert archive_mod.extract_argv("/a/b.txt", "/dst") is None


def test_create_argv_targz():
    argv = archive_mod.create_argv("/out.tar.gz", ["src"])
    assert argv == ["tar", "-czf", "/out.tar.gz", "src"]


def test_create_argv_zip():
    argv = archive_mod.create_argv("/out.zip", ["a", "b"])
    assert argv == ["zip", "-r", "/out.zip", "a", "b"]


def test_create_argv_unsupported_rar():
    # We deliberately don't offer rar creation.
    assert archive_mod.create_argv("/out.rar", ["src"]) is None


def test_archive_menu_items_for_archive_file(tmp_path):
    archive = tmp_path / "x.tar.gz"
    archive.write_bytes(b"")
    plugin = archive_mod.ArchivePlugin()
    labels = [label for label, _cb in plugin.get_menu_items(str(archive))]
    assert "Extract Here" in labels
    assert "Extract To..." in labels
    assert "Create Archive..." in labels


def test_archive_menu_items_for_plain_file(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("hi")
    plugin = archive_mod.ArchivePlugin()
    labels = [label for label, _cb in plugin.get_menu_items(str(f))]
    assert "Extract Here" not in labels
    assert "Create Archive..." in labels


# ---------------------------------------------------------------------------
# archive — pre-extraction path containment (defense-in-depth)
# ---------------------------------------------------------------------------


def _write_tar(path, entries):
    """``entries`` = list of (name, kind, linkname). kind in {file,sym,lnk}."""
    with tarfile.open(path, "w") as tf:
        for name, kind, linkname in entries:
            if kind == "file":
                data = b"x"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            elif kind == "sym":
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = linkname
                tf.addfile(info)
            elif kind == "lnk":
                info = tarfile.TarInfo(name)
                info.type = tarfile.LNKTYPE
                info.linkname = linkname
                tf.addfile(info)


def test_unsafe_members_clean_tar(tmp_path):
    arc = tmp_path / "ok.tar"
    _write_tar(arc, [("a/b.txt", "file", ""), ("a/c.txt", "file", "")])
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_dotdot_tar(tmp_path):
    arc = tmp_path / "bad.tar"
    _write_tar(arc, [("../escape.txt", "file", "")])
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["../escape.txt"]


def test_unsafe_members_absolute_tar(tmp_path):
    arc = tmp_path / "abs.tar"
    # tarfile stores the name as-is; an absolute name is unsafe.
    _write_tar(arc, [("/etc/passwd", "file", "")])
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["/etc/passwd"]


def test_unsafe_members_symlink_escape_tar(tmp_path):
    arc = tmp_path / "sym.tar"
    # link sits at dst/link, target ../../outside escapes dst.
    _write_tar(arc, [("link", "sym", "../../outside")])
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["link -> ../../outside"]


def test_unsafe_members_absolute_symlink_tar(tmp_path):
    arc = tmp_path / "abssym.tar"
    _write_tar(arc, [("link", "sym", "/etc")])
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["link -> /etc"]


def test_unsafe_members_hardlink_escape_tar(tmp_path):
    arc = tmp_path / "lnk.tar"
    _write_tar(arc, [("hl", "lnk", "../../etc/shadow")])
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["hl -> ../../etc/shadow"]


def test_unsafe_members_hardlink_resolves_from_root(tmp_path):
    # POSIX/tar hardlink targets resolve from the extraction ROOT, not the
    # link member's directory. `sub/hl -> ../outside` therefore escapes
    # (dest/../outside), even though a symlink with the same target would
    # stay inside (dest/sub/../outside == dest/outside).
    arc = tmp_path / "hlroot.tar"
    _write_tar(arc, [("sub/hl", "lnk", "../outside")])
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["sub/hl -> ../outside"]


def test_safe_hardlink_within_root(tmp_path):
    # `hl -> target` resolves to dest/target — inside, so safe.
    arc = tmp_path / "hlok.tar"
    _write_tar(arc, [("hl", "lnk", "target")])
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_safe_relative_symlink_within_tar(tmp_path):
    arc = tmp_path / "innersym.tar"
    # sub/link -> ../target  resolves to dst/target, which stays inside.
    _write_tar(arc, [("sub/link", "sym", "../target")])
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_clean_zip(tmp_path):
    arc = tmp_path / "ok.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("a/b.txt", "hi")
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_dotdot_zip(tmp_path):
    arc = tmp_path / "bad.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("../escape.txt", "hi")
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["../escape.txt"]


def _write_zip_symlink(path, name, target):
    """Write a zip with one unix-mode symlink entry (target = content)."""
    import stat as _stat
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.external_attr = (_stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, target)


def test_unsafe_members_zip_symlink_escape(tmp_path):
    arc = tmp_path / "sym.zip"
    _write_zip_symlink(arc, "link", "../../outside")
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["link -> ../../outside"]


def test_unsafe_members_zip_symlink_absolute(tmp_path):
    arc = tmp_path / "abssym.zip"
    _write_zip_symlink(arc, "link", "/etc/passwd")
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["link -> /etc/passwd"]


def test_safe_zip_symlink_within(tmp_path):
    arc = tmp_path / "innersym.zip"
    _write_zip_symlink(arc, "sub/link", "../target")
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_zip_oversize_symlink(tmp_path):
    # A member marked as a symlink but carrying a huge payload is refused
    # without reading it (DoS guard).
    arc = tmp_path / "big.zip"
    _write_zip_symlink(arc, "link", "x" * (archive_mod._MAX_SYMLINK_BYTES + 1))
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["link -> <oversize link>"]


def test_unsafe_members_backslash_zip(tmp_path):
    arc = tmp_path / "bs.zip"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.writestr("..\\..\\escape.txt", "hi")
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["..\\..\\escape.txt"]


def test_unsafe_members_unreadable_fails_closed(tmp_path):
    arc = tmp_path / "broken.tar.gz"
    arc.write_bytes(b"not a real gzip tar")
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["<unreadable archive>"]


def test_unsafe_members_7z_not_validated(tmp_path):
    # No stdlib reader for 7z: we don't validate, so return empty even though
    # the file is bogus (extractor remains responsible).
    arc = tmp_path / "x.7z"
    arc.write_bytes(b"garbage")
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_tar_zst_not_validated(tmp_path):
    # tarfile cannot decompress zstd before Python 3.14; we must NOT
    # fail-closed on a valid archive the external tar handles fine. Even a
    # malicious-looking name inside is left to the extractor here.
    import subprocess

    src = tmp_path / "a.txt"
    src.write_text("hi")
    arc = tmp_path / "x.tar.zst"
    rc = subprocess.run(
        ["tar", "--zstd", "-cf", str(arc), "-C", str(tmp_path), "a.txt"]
    ).returncode
    if rc != 0 or not arc.exists():
        pytest.skip("tar --zstd unavailable")
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_clean_targz_introspected(tmp_path):
    arc = tmp_path / "ok.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        info = tarfile.TarInfo("a/b.txt")
        data = b"x"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    assert archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst")) == []


def test_unsafe_members_dotdot_targz(tmp_path):
    arc = tmp_path / "bad.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        data = b"x"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    bad = archive_mod.archive_unsafe_members(str(arc), str(tmp_path / "dst"))
    assert bad == ["../escape.txt"]


def test_unsafe_members_unknown_format(tmp_path):
    f = tmp_path / "plain.txt"
    f.write_text("hi")
    assert archive_mod.archive_unsafe_members(str(f), str(tmp_path / "dst")) == []


def test_extract_into_aborts_on_unsafe(tmp_path):
    arc = tmp_path / "evil.tar"
    _write_tar(arc, [("../escape.txt", "file", "")])
    plugin = archive_mod.ArchivePlugin()
    with patch.object(plugin, "_run") as run, \
            patch.object(archive_mod.ArchivePlugin, "_warn") as warn:
        plugin._extract_into(str(arc), str(tmp_path / "dst"))
    run.assert_not_called()
    warn.assert_called_once()


def test_extract_into_runs_on_safe(tmp_path):
    arc = tmp_path / "good.tar"
    _write_tar(arc, [("a/b.txt", "file", "")])
    plugin = archive_mod.ArchivePlugin()
    with patch.object(plugin, "_run") as run:
        plugin._extract_into(str(arc), str(tmp_path / "dst"))
    run.assert_called_once()


# ---------------------------------------------------------------------------
# remote_copy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dest, scp, ftp", [
    ("user@host:/tmp/foo", True, False),
    ("host:/tmp/foo", True, False),
    ("ftp://host/dir/", False, True),
    ("ftps://user:pw@host/path", False, True),
    ("/local/path", False, False),
    ("relative/path", False, False),
])
def test_remote_dest_classification(dest, scp, ftp):
    assert rc_mod.is_scp_dest(dest) is scp
    assert rc_mod.is_ftp_dest(dest) is ftp


def test_scp_argv_shape():
    argv = rc_mod.scp_argv("/src/file", "user@host:/dst")
    assert argv[0] == "scp"
    assert "/src/file" in argv
    assert "user@host:/dst" in argv
    assert "-r" in argv[1]  # -rp


def test_sftp_batch_argv_builds_script():
    built = rc_mod.sftp_batch_argv("/src/file", "user@host:/dst/path")
    assert built is not None
    argv, script = built
    assert argv[:3] == ["sftp", "-b", "-"]
    assert argv[-1] == "user@host"
    assert "/src/file" in script
    assert "/dst/path" in script
    assert script.startswith("put ")


def test_sftp_batch_argv_rejects_non_scp_dest():
    assert rc_mod.sftp_batch_argv("/src", "/local/path") is None
    assert rc_mod.sftp_batch_argv("/src", "ftp://host/") is None


def test_lftp_argv_file_upload(tmp_path):
    f = tmp_path / "thing.txt"
    f.write_text("x")
    argv = rc_mod.lftp_argv(str(f), "ftp://host/incoming/")
    assert argv is not None
    assert argv[0] == "lftp"
    assert argv[-1] == "ftp://host"
    # The script is the -e payload.
    script = argv[2]
    assert "cd " in script
    assert "/incoming/" in script
    assert "put" in script
    assert "thing.txt" in script


def test_lftp_argv_rejects_password_in_url(tmp_path):
    f = tmp_path / "thing.txt"
    f.write_text("x")
    assert rc_mod.lftp_argv(str(f), "ftp://user:pw@host/incoming/") is None


def test_lftp_argv_allows_user_without_password(tmp_path):
    f = tmp_path / "thing.txt"
    f.write_text("x")
    argv = rc_mod.lftp_argv(str(f), "ftp://user@host/incoming/")
    assert argv is not None
    assert argv[-1] == "ftp://user@host"


def test_lftp_argv_rejects_non_ftp():
    assert rc_mod.lftp_argv("/src", "user@host:/dst") is None


# ---------------------------------------------------------------------------
# rsync_sync
# ---------------------------------------------------------------------------

def test_rsync_argv_basic_copy():
    argv = rs_mod.rsync_argv("/src/", "/dst/")
    assert argv[0] == "rsync"
    # Flags that deliver "resumable copy":
    for flag in ("--partial", "--append-verify", "--inplace", "-a"):
        assert flag in argv, f"missing {flag}: {argv}"
    assert argv[-2:] == ["/src/", "/dst/"]
    assert "--remove-source-files" not in argv
    assert "--dry-run" not in argv


def test_rsync_argv_move_appends_remove_source():
    argv = rs_mod.rsync_argv("/src/", "/dst/", move=True)
    assert "--remove-source-files" in argv


def test_rsync_argv_dry_run():
    argv = rs_mod.rsync_argv("/src/", "/dst/", dry_run=True)
    assert "--dry-run" in argv


def test_rsync_argv_remote_dest_passes_through():
    argv = rs_mod.rsync_argv("/src/", "user@host:/dst/")
    assert argv[-1] == "user@host:/dst/"


def test_rsync_argv_dash_path_after_terminator():
    # A leading-dash source (e.g. ``-e sh -c '…'``) must be a path, not an
    # rsync option: it has to appear after the ``--`` terminator.
    argv = rs_mod.rsync_argv("-e sh -c evil", "/dst/")
    assert "--" in argv
    assert argv.index("--") < argv.index("-e sh -c evil")
    assert argv[-2:] == ["-e sh -c evil", "/dst/"]


def test_scp_argv_leading_dash_source_made_safe():
    # scp has no ``--``; a leading-dash local source is prefixed with ``./``.
    argv = rc_mod.scp_argv("-oProxyCommand=evil", "user@host:/dst")
    assert "-oProxyCommand=evil" not in argv
    assert "./-oProxyCommand=evil" in argv
    assert argv[-1] == "user@host:/dst"


def test_scp_argv_leading_dash_source_with_colon_made_safe():
    # A colon in the local filename must NOT let it masquerade as a remote
    # spec and slip through unprefixed (would be an scp -o option → RCE).
    argv = rc_mod.scp_argv("-oProxyCommand=sh:foo", "user@host:/dst")
    assert "-oProxyCommand=sh:foo" not in argv
    assert "./-oProxyCommand=sh:foo" in argv
    assert argv[-1] == "user@host:/dst"


def test_scp_argv_ordinary_source_untouched():
    # A normal local source (no leading dash) is passed through verbatim.
    argv = rc_mod.scp_argv("/src/file", "user@host:/dst")
    assert "/src/file" in argv
    assert "./-" not in " ".join(argv)


# ---------------------------------------------------------------------------
# checksum
# ---------------------------------------------------------------------------

def test_hash_file_matches_hashlib(tmp_path):
    f = tmp_path / "data.bin"
    payload = b"the quick brown fox" * 1000
    f.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert checksum_mod.hash_file(str(f), "sha256") == expected


def test_hash_file_md5(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello")
    assert checksum_mod.hash_file(str(f), "md5") == hashlib.md5(b"hello").hexdigest()


def test_checksum_menu_items_only_for_files(tmp_path):
    plugin = checksum_mod.ChecksumPlugin()
    f = tmp_path / "a"
    f.write_text("x")
    labels = [lbl for lbl, _cb in plugin.get_menu_items(str(f))]
    assert "MD5 Sum" in labels
    # Directories: no items.
    assert plugin.get_menu_items(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# multi_rename
# ---------------------------------------------------------------------------

def test_apply_template_name_and_ext():
    assert mr_mod.apply_template("[N][E]", "foo.txt", 0) == "foo.txt"


def test_apply_template_counter_default_pad():
    assert mr_mod.apply_template("[C]_[N][E]", "foo.txt", 4) == "005_foo.txt"


def test_apply_template_counter_custom_pad():
    assert mr_mod.apply_template("[C:2]_[N][E]", "foo.txt", 0) == "01_foo.txt"


def test_apply_template_search_replace():
    out = mr_mod.apply_template(
        "[N][E]", "draft_thing.txt", 0, search="draft_", replace="",
    )
    assert out == "thing.txt"


def test_apply_template_regex():
    out = mr_mod.apply_template(
        "[N][E]", "img_001.jpg", 0,
        search=r"^img_(\d+)$", replace=r"photo_\1", regex=True,
    )
    assert out == "photo_001.jpg"


def test_apply_template_bad_regex_falls_through():
    # Unbalanced paren — should not raise, just leave base unchanged.
    out = mr_mod.apply_template(
        "[N][E]", "a.txt", 0, search="(", replace="x", regex=True,
    )
    assert out == "a.txt"


def test_plan_renames_skips_identity():
    plan = mr_mod.plan_renames(
        "/d", ["a.txt", "b.txt"], "[N][E]",
    )
    assert plan == []


def test_plan_renames_uses_counter_index():
    plan = mr_mod.plan_renames(
        "/d", ["a.txt", "b.txt"], "[C:2]_[N][E]",
    )
    assert plan == [
        ("/d/a.txt", "/d/01_a.txt"),
        ("/d/b.txt", "/d/02_b.txt"),
    ]


def test_plan_renames_real_filesystem(tmp_path):
    (tmp_path / "draft_one.txt").write_text("x")
    (tmp_path / "draft_two.txt").write_text("y")
    names = sorted(os.listdir(tmp_path))
    plan = mr_mod.plan_renames(
        str(tmp_path), names, "[N][E]", search="draft_", replace="",
    )
    new_basenames = sorted(os.path.basename(new) for _old, new in plan)
    assert new_basenames == ["one.txt", "two.txt"]


# multi_rename: collision-safe plan application -------------------------------


def test_apply_rename_plan_swap_is_nondestructive(tmp_path):
    """a<->b swap must not destroy either file (naive os.rename would)."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("AAA")
    b.write_text("BBB")
    plan = [(str(a), str(b)), (str(b), str(a))]
    failures = mr_mod._apply_rename_plan(plan)
    assert failures == []
    assert (tmp_path / "a.txt").read_text() == "BBB"
    assert (tmp_path / "b.txt").read_text() == "AAA"


def test_apply_rename_plan_chain_is_nondestructive(tmp_path):
    """a->b, b->c chain preserves all content."""
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("AAA")
    b.write_text("BBB")
    plan = [(str(a), str(b)), (str(b), str(tmp_path / "c.txt"))]
    failures = mr_mod._apply_rename_plan(plan)
    assert failures == []
    assert (tmp_path / "b.txt").read_text() == "AAA"
    assert (tmp_path / "c.txt").read_text() == "BBB"
    assert not (tmp_path / "a.txt").exists()


def test_apply_rename_plan_leaves_no_temp_files(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("x")
    plan = [(str(a), str(tmp_path / "renamed.txt"))]
    assert mr_mod._apply_rename_plan(plan) == []
    leftovers = [p for p in os.listdir(tmp_path) if ".qfm-rename-" in p]
    assert leftovers == []
    assert (tmp_path / "renamed.txt").read_text() == "x"


def test_apply_rename_plan_duplicate_finals_rejected(tmp_path):
    """Two sources mapping to the same name must not silently drop one."""
    a = tmp_path / "a.txt"; a.write_text("AAA")
    b = tmp_path / "b.txt"; b.write_text("BBB")
    x = str(tmp_path / "x.txt")
    failures = mr_mod._apply_rename_plan([(str(a), x), (str(b), x)])
    # Both colliding entries are reported; neither original is destroyed.
    assert len(failures) == 2
    assert a.read_text() == "AAA"
    assert b.read_text() == "BBB"
    assert not (tmp_path / "x.txt").exists()


def test_apply_rename_plan_temp_lookalike_bystander_preserved(tmp_path):
    """A real file living in the target dir must never be eaten as scratch."""
    a = tmp_path / "a.txt"; a.write_text("AAA")
    # A file that resembles an old-style temp name.
    bystander = tmp_path / "x.txt.qfm-rename-1-0.tmp"
    bystander.write_text("KEEP")
    failures = mr_mod._apply_rename_plan([(str(a), str(tmp_path / "x.txt"))])
    assert failures == []
    assert bystander.read_text() == "KEEP"
    assert (tmp_path / "x.txt").read_text() == "AAA"


def test_apply_rename_plan_rolls_back_on_staging_failure(tmp_path):
    """If staging one source fails, already-staged sources are restored and
    no surviving original is clobbered by phase 2."""
    a = tmp_path / "a.txt"; a.write_text("AAA")
    b = tmp_path / "b.txt"; b.write_text("BBB")
    # Plan: a->b (would need b staged away first), b->c. Force b's staging to
    # fail; a must be rolled back and original b must survive intact.
    plan = [(str(a), str(b)), (str(b), str(tmp_path / "c.txt"))]

    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        # Fail the second staging move (b -> scratch); allow rollback moves.
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated staging failure")
        return real_rename(src, dst)

    with patch("qfileman.plugins.builtin.multi_rename.os.rename",
               side_effect=flaky_rename):
        failures = mr_mod._apply_rename_plan(plan)

    assert failures  # the failure was reported
    # Critically: original b is NOT destroyed, and a is back in place.
    assert a.read_text() == "AAA"
    assert b.read_text() == "BBB"
    assert not (tmp_path / "c.txt").exists()


def test_apply_rename_plan_case_insensitive_duplicate_rejected(tmp_path):
    """On a case-insensitive fs, a->X and b->x denote one entry; both must be
    rejected rather than letting the second os.replace eat the first."""
    a = tmp_path / "a.txt"; a.write_text("AAA")
    b = tmp_path / "b.txt"; b.write_text("BBB")
    plan = [(str(a), str(tmp_path / "X.txt")), (str(b), str(tmp_path / "x.txt"))]
    # Simulate case-insensitive normcase (POSIX normcase is a no-op).
    with patch("qfileman.plugins.builtin.multi_rename.os.path.normcase",
               side_effect=lambda p: p.lower()):
        failures = mr_mod._apply_rename_plan(plan)
    assert len(failures) == 2
    assert a.read_text() == "AAA"
    assert b.read_text() == "BBB"


def test_apply_rename_plan_runnable_must_not_eat_rejected_source(tmp_path):
    """A runnable entry whose target is the surviving source of a rejected
    duplicate (c->a when a->x,b->x were rejected) must NOT clobber it."""
    a = tmp_path / "a.txt"; a.write_text("AAA")
    b = tmp_path / "b.txt"; b.write_text("BBB")
    c = tmp_path / "c.txt"; c.write_text("CCC")
    x = str(tmp_path / "x.txt")
    plan = [(str(a), x), (str(b), x), (str(c), str(a))]
    failures = mr_mod._apply_rename_plan(plan)
    assert len(failures) == 3
    assert a.read_text() == "AAA"   # not silently overwritten by c
    assert b.read_text() == "BBB"
    assert c.read_text() == "CCC"
    assert not (tmp_path / "x.txt").exists()
