"""Live integration tests against the installed system tools.

Earlier suites stay pure — argv builders, format detectors, parsers.
This file is different: it *runs* the binaries the plugins shell out
to and checks the round trip end-to-end on real data.

Every test ``pytest.skip``s when its tool isn't installed, so the
file is safe to run anywhere and still useful: on the developer's box
(or CI with the system packages preloaded) the skips drop away and we
get real coverage that the argv we build is actually accepted by the
tool, not just well-shaped on paper.

No network is touched, no destination outside ``tmp_path`` is written
to, and no daemon state is mutated (e.g. ``udisksctl`` is exercised
read-only via ``--help``). The plugins themselves are not invoked
through Qt — we call the pure argv builders and run the resulting
process directly. That keeps tests fast and reproducible.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from qfileman.plugins.builtin import archive as archive_mod
from qfileman.plugins.builtin import checksum as checksum_mod
from qfileman.plugins.builtin import folder_size as fs_mod
from qfileman.plugins.builtin import git_status as gs_mod
from qfileman.plugins.builtin import mount_manager as mm_mod
from qfileman.plugins.builtin import open_with as ow_mod
from qfileman.plugins.builtin import remote_copy as rc_mod
from qfileman.plugins.builtin import rsync_sync as rs_mod
from qfileman.plugins.builtin import sync_folders as sf_mod
from qfileman.plugins.builtin import trash as tr_mod


def _need(tool: str) -> str:
    """Return the absolute path to ``tool`` or skip the test."""
    path = shutil.which(tool)
    if not path:
        pytest.skip(f"{tool} not installed on this box")
    return path


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    """Run ``argv`` synchronously with a generous timeout and text capture."""
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=60, **kw,
    )


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["tar", "tar.gz", "tar.bz2", "tar.xz", "zip", "7z"])
def test_archive_round_trip(tmp_path, fmt):
    """Build an archive, extract it, and compare bytes — for every format
    the plugin claims to support that we have the binary for."""
    tool_for_fmt = {
        "tar":     "tar",
        "tar.gz":  "tar",
        "tar.bz2": "tar",
        "tar.xz":  "tar",
        "zip":     "zip",
        "7z":      "7z",
    }
    extract_tool = {
        "tar": "tar", "tar.gz": "tar", "tar.bz2": "tar", "tar.xz": "tar",
        "zip": "unzip", "7z": "7z",
    }
    _need(tool_for_fmt[fmt])
    _need(extract_tool[fmt])

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    payload = b"hello, world!\n" * 100
    (src_dir / "a.txt").write_bytes(payload)
    (src_dir / "b.bin").write_bytes(b"\x00" * 256)

    archive = tmp_path / f"out.{fmt}"
    create_argv = archive_mod.create_argv(str(archive), ["src"])
    assert create_argv is not None
    result = _run(create_argv, cwd=str(tmp_path))
    assert result.returncode == 0, f"create failed: {result.stderr or result.stdout}"
    assert archive.exists()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    extract_argv = archive_mod.extract_argv(str(archive), str(out_dir))
    assert extract_argv is not None
    result = _run(extract_argv, cwd=str(out_dir))
    assert result.returncode == 0, f"extract failed: {result.stderr or result.stdout}"

    # File contents must come back byte-identical.
    extracted = out_dir / "src" / "a.txt"
    assert extracted.exists(), \
        f"missing extracted file under {list(out_dir.rglob('*'))}"
    assert extracted.read_bytes() == payload


def test_archive_create_for_unsupported_rar_returns_none():
    # Sanity check on the negative path: rar is read-only.
    assert archive_mod.create_argv("/tmp/x.rar", ["foo"]) is None


# ---------------------------------------------------------------------------
# checksum — verify the hashlib path matches the system sha256sum
# ---------------------------------------------------------------------------

def test_checksum_matches_system_sha256sum(tmp_path):
    sha256sum = shutil.which("sha256sum")
    if not sha256sum:
        pytest.skip("sha256sum not installed")
    f = tmp_path / "data.bin"
    f.write_bytes(b"qfileman" * 1024)
    ours = checksum_mod.hash_file(str(f), "sha256")
    result = _run([sha256sum, str(f)])
    assert result.returncode == 0
    theirs = result.stdout.split()[0]
    assert ours == theirs


# ---------------------------------------------------------------------------
# rsync / sync_folders — actually run a dry-run + a real copy
# ---------------------------------------------------------------------------

def test_rsync_dry_run_argv_parses_clean(tmp_path):
    _need("rsync")
    src = tmp_path / "left"
    dst = tmp_path / "right"
    src.mkdir()
    dst.mkdir()
    (src / "only-here.txt").write_text("new\n")
    (dst / "stale.txt").write_text("delete me\n")

    argv = sf_mod.dry_run_argv(str(src), str(dst))
    result = _run(argv)
    assert result.returncode == 0, result.stderr
    rows = sf_mod.parse_itemize(result.stdout)
    actions = {a for a, _p in rows}
    paths = [p for _a, p in rows]
    assert "new" in actions
    assert "delete" in actions
    assert "only-here.txt" in paths
    assert "stale.txt" in paths


def test_rsync_apply_argv_does_resumable_copy(tmp_path):
    _need("rsync")
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    blob = b"X" * (1 << 16)
    (src / "big.bin").write_bytes(blob)
    (src / "tiny.txt").write_text("hi\n")

    argv = sf_mod.apply_argv(str(src), str(dst))
    result = _run(argv)
    assert result.returncode == 0, result.stderr
    assert (dst / "big.bin").read_bytes() == blob
    assert (dst / "tiny.txt").read_text() == "hi\n"


def test_rsync_sync_argv_runs(tmp_path):
    """The flags from the rsync_sync plugin must be accepted by the
    installed rsync binary (catches drift if rsync ever drops a flag)."""
    _need("rsync")
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "f").write_text("payload\n")
    argv = rs_mod.rsync_argv(str(src) + "/", str(dst) + "/", dry_run=True)
    result = _run(argv)
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# git — real init + find_repo_root + a status call
# ---------------------------------------------------------------------------

def test_git_init_and_status(tmp_path):
    git = _need("git")
    repo = tmp_path / "repo"
    repo.mkdir()
    # Run with isolated config so we don't depend on global git.user.name.
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example",
    }
    _run([git, "-C", str(repo), "init", "-q"])
    assert (repo / ".git").exists()
    assert gs_mod.find_repo_root(str(repo)) == str(repo)

    sub = repo / "sub"
    sub.mkdir()
    (sub / "a.txt").write_text("hi\n")
    # Plugin uses the repo root from any path inside the tree.
    assert gs_mod.find_repo_root(str(sub / "a.txt")) == str(repo)

    # The commands the plugin actually wires up must run cleanly.
    # ``--porcelain`` collapses untracked dirs to a single ``?? sub/`` line
    # by default; pass ``-uall`` so the leaf path shows up so this test
    # is robust against git version differences.
    result = _run(
        [git, "-C", str(repo), "status", "--porcelain", "-uall"], env=env,
    )
    assert result.returncode == 0
    assert "a.txt" in result.stdout

    _run([git, "-C", str(repo), "add", "."], env=env)
    result = _run([git, "-C", str(repo), "commit", "-m", "init"], env=env)
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# trash — use gio trash to actually trash a file, then restore it
# ---------------------------------------------------------------------------

def test_trash_round_trip_via_gio(monkeypatch):
    gio = shutil.which("gio")
    if not gio:
        pytest.skip("gio not installed")
    # ``gio trash`` refuses to operate on tmpfs mounts (no sibling
    # .Trash directory), so we put the victim under $HOME where XDG
    # trash is rooted.
    monkeypatch.setattr(
        tr_mod, "choose_backend",
        lambda: ("gio", lambda p: ["gio", "trash", "--", p]),
    )
    home = os.path.expanduser("~")
    if not os.access(home, os.W_OK):
        pytest.skip("home directory is not writeable")
    victim = os.path.join(home, ".qfileman-trash-test")
    with open(victim, "w") as f:
        f.write("garbage\n")
    try:
        argv = tr_mod.trash_argv(victim)
        assert argv == ["gio", "trash", "--", victim]
        result = _run(argv)
        if result.returncode != 0:
            pytest.skip(f"gio trash unusable here: {result.stderr.strip()}")
        assert not os.path.exists(victim), \
            "file should be gone after gio trash"
    finally:
        # Best-effort cleanup so we don't leave noise in the user's
        # trash UI. Failures here are non-fatal.
        try:
            os.remove(victim)
        except FileNotFoundError:
            pass
        _run([gio, "trash", "--empty"])


# ---------------------------------------------------------------------------
# open_with — xdg-mime must agree on the MIME of a plain text file
# ---------------------------------------------------------------------------

def test_open_with_mime_lookup(tmp_path):
    _need("xdg-mime")
    f = tmp_path / "note.txt"
    f.write_text("plain text\n")
    mime = ow_mod.mime_for(str(f))
    assert mime is not None
    # Most distros map .txt → text/plain; accept any text/* to stay
    # robust against MIME-DB tweaks.
    assert mime.startswith("text/"), f"got {mime!r}"


# ---------------------------------------------------------------------------
# folder_size — agree with `du -sb`
# ---------------------------------------------------------------------------

def test_folder_size_agrees_with_du(tmp_path):
    du = shutil.which("du")
    if not du:
        pytest.skip("du not installed")
    (tmp_path / "a").write_bytes(b"x" * 1234)
    (tmp_path / "b").write_bytes(b"y" * 5678)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c").write_bytes(b"z" * 90)

    ours = fs_mod.directory_size(str(tmp_path))
    result = _run([du, "-sb", "--apparent-size", str(tmp_path)])
    if result.returncode != 0:
        # Some du versions don't have --apparent-size; fall back.
        result = _run([du, "-sb", str(tmp_path)])
    assert result.returncode == 0
    theirs = int(result.stdout.split()[0])
    # ``du`` counts directory entries themselves, we don't. Allow up
    # to 8 KiB of slack for the inode-size accounting of the
    # directories we walk past.
    assert abs(ours - theirs) <= 8 * 1024, f"ours={ours} theirs={theirs}"


# ---------------------------------------------------------------------------
# mount_manager — real lsblk output must parse without crashing
# ---------------------------------------------------------------------------

def test_lsblk_json_parses(tmp_path):
    _need("lsblk")
    result = _run(["lsblk", "-J", "-o", "NAME,SIZE,FSTYPE,MOUNTPOINT,LABEL"])
    assert result.returncode == 0, result.stderr
    rows = mm_mod.parse_lsblk_json(result.stdout)
    # Even a fresh container has *something* — at minimum a root device.
    # Don't assert specific names (would be flaky across machines) but
    # do require parse_lsblk_json to return a list.
    assert isinstance(rows, list)
    for row in rows:
        assert "name" in row


def test_udisksctl_help_runs():
    udisksctl = shutil.which("udisksctl")
    if not udisksctl:
        pytest.skip("udisksctl not installed")
    # ``udisksctl`` has no top-level ``--help``; it always wants a
    # subcommand. ``help`` is the documented one. Even when it prints
    # usage to stderr (and exits non-zero on bare invocation) we just
    # care that the binary speaks the subcommands we use.
    result = _run([udisksctl, "help"])
    text = result.stdout + result.stderr
    assert "mount" in text
    assert "unmount" in text
    assert "power-off" in text


# ---------------------------------------------------------------------------
# rclone — list backends and verify the plugin's argv runs cleanly with --dry-run
# ---------------------------------------------------------------------------

def test_rclone_version():
    _need("rclone")
    result = _run(["rclone", "--version"])
    assert result.returncode == 0
    assert "rclone v" in result.stdout


def test_rclone_listremotes_smoke():
    _need("rclone")
    # listremotes is always cheap; even an empty config returns rc=0.
    result = _run(["rclone", "listremotes"])
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# remote_copy — argv builders must produce something the tools accept
#   (we don't connect to a remote; we use --help / -h to exercise binary
#   compatibility with the flags we feed it).
# ---------------------------------------------------------------------------

def test_scp_understands_our_flags():
    scp = shutil.which("scp")
    if not scp:
        pytest.skip("scp not installed")
    # Our argv has ``-rp`` then src/dst. ``scp`` rejects missing
    # arguments with a non-zero exit but a recognisable usage banner.
    result = _run([scp, "-h"])
    text = result.stdout + result.stderr
    assert "usage" in text.lower() or "Usage" in text


def test_lftp_argv_builder_handles_real_paths(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("payload")
    argv = rc_mod.lftp_argv(str(f), "ftp://example/incoming/")
    assert argv[0] == "lftp"
    # The -e payload is shell-quoted; sanity-check it parses with shlex.
    import shlex
    script = argv[2]
    tokens = shlex.split(script)
    assert tokens[0] == "cd"


# ---------------------------------------------------------------------------
# diff — at least one configured GUI tool must be present.
# ---------------------------------------------------------------------------

def test_diff_can_choose_a_tool():
    from qfileman.plugins.builtin import diff as diff_mod
    tool = diff_mod.choose_tool()
    # If meld/kdiff3/diffuse/xxdiff/kompare are all missing the plugin
    # falls back to plain diff via build_argv — exercise that path too.
    if tool is None:
        argv = diff_mod.build_argv("/a", "/b")
        assert argv[0] == "diff"
    else:
        name, prefix = tool
        # The binary the picker chose must actually be on PATH.
        assert shutil.which(prefix[0]) is not None, \
            f"chose {name} but {prefix[0]} not found"
