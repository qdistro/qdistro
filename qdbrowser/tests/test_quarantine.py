"""Quarantine store: SQLite metadata, sidecar, scan, polkit-gated release.

Pure-Python tests — no Qt is involved. The polkit check is mocked by
patching ``subprocess.run``.
"""

import hashlib
import json
import os
import sqlite3
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# QuarantineStore basics
# ---------------------------------------------------------------------------


def _make_store(tmp_path):
    from qdbrowser.quarantine import QuarantineStore
    return QuarantineStore(str(tmp_path / "quar"))


def test_store_creates_dir_and_db(tmp_path):
    store = _make_store(tmp_path)
    assert os.path.isdir(store.directory)
    assert os.path.exists(store.db_path)
    # Schema is present.
    con = sqlite3.connect(store.db_path)
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    con.close()
    assert any(r[0] == "downloads" for r in rows)
    store.close()


def test_record_inserts_row(tmp_path):
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "foo.bin")
    open(qpath, "wb").close()
    row_id = store.record(
        quarantine_path=qpath,
        filename="foo.bin",
        source_url="https://example.com/foo.bin",
        content_type="application/octet-stream",
        profile_name="default",
        tab_id=3,
        size_bytes=0,
        sha256="abc123",
        scan_result="pending",
    )
    assert row_id is not None
    row = store.get(row_id)
    assert row["source_url"] == "https://example.com/foo.bin"
    assert row["scan_result"] == "pending"
    assert row["profile_name"] == "default"
    assert row["tab_id"] == 3
    assert row["released"] == 0
    store.close()


def test_write_sidecar_produces_json(tmp_path):
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "a.txt")
    open(qpath, "wb").close()
    row_id = store.record(quarantine_path=qpath, filename="a.txt",
                          source_url="https://x/")
    payload = {"source_url": "https://x/", "extra": ["a", "b"]}
    sidecar = store.write_sidecar(row_id, qpath, payload)
    assert sidecar.endswith(".qdistro-meta.json")
    with open(sidecar) as f:
        loaded = json.load(f)
    assert loaded == payload
    store.close()


def test_update_after_finish_and_scan(tmp_path):
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "z.bin")
    open(qpath, "wb").close()
    row_id = store.record(quarantine_path=qpath, filename="z.bin",
                          source_url="https://x/z")
    store.update_after_finish(row_id, sha256="deadbeef", size_bytes=42)
    store.update_scan_result(row_id, "clean")
    row = store.get(row_id)
    assert row["sha256"] == "deadbeef"
    assert row["size_bytes"] == 42
    assert row["scan_result"] == "clean"
    store.close()


def test_list_pending_and_all(tmp_path):
    store = _make_store(tmp_path)
    for n in range(3):
        p = os.path.join(store.directory, f"f{n}.bin")
        open(p, "wb").close()
        store.record(quarantine_path=p, filename=f"f{n}.bin",
                     source_url=f"https://x/{n}")
    pending = store.list_pending()
    assert len(pending) == 3
    all_rows = store.list_all(limit=10)
    assert len(all_rows) == 3
    store.close()


def test_plan_path_handles_collisions(tmp_path):
    store = _make_store(tmp_path)
    p1 = store.plan_path("dup.txt")
    open(p1, "wb").close()
    p2 = store.plan_path("dup.txt")
    assert p1 != p2
    assert p2.endswith(".1.txt") or p2.endswith(".txt")
    open(p2, "wb").close()
    p3 = store.plan_path("dup.txt")
    assert p3 not in (p1, p2)
    store.close()


# ---------------------------------------------------------------------------
# hash_file
# ---------------------------------------------------------------------------


def test_hash_file_matches_hashlib(tmp_path):
    from qdbrowser.quarantine import hash_file
    data = os.urandom(1024 * 5 + 17)
    p = tmp_path / "blob.bin"
    p.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert hash_file(str(p)) == expected
    # Small chunk size still produces the right hash.
    assert hash_file(str(p), chunk=7) == expected


def test_hash_file_empty(tmp_path):
    from qdbrowser.quarantine import hash_file
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert hash_file(str(p)) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# run_scan
# ---------------------------------------------------------------------------


def test_run_scan_skipped_on_empty_command(tmp_path):
    from qdbrowser.quarantine import run_scan
    p = tmp_path / "x"
    p.write_bytes(b"hi")
    assert run_scan("", str(p)) == "skipped"


def test_run_scan_clean_with_true(tmp_path):
    from qdbrowser.quarantine import run_scan
    p = tmp_path / "x"
    p.write_bytes(b"hi")
    if not os.path.exists("/bin/true"):
        pytest.skip("/bin/true not present")
    assert run_scan("/bin/true", str(p)) == "clean"


def test_run_scan_bad_with_false(tmp_path):
    from qdbrowser.quarantine import run_scan
    p = tmp_path / "x"
    p.write_bytes(b"hi")
    if not os.path.exists("/bin/false"):
        pytest.skip("/bin/false not present")
    assert run_scan("/bin/false", str(p)) == "bad"


def test_run_scan_error_when_command_missing(tmp_path):
    from qdbrowser.quarantine import run_scan
    p = tmp_path / "x"
    p.write_bytes(b"hi")
    assert run_scan("/nonexistent/scanner-xyzzy", str(p)) == "error"


# ---------------------------------------------------------------------------
# release + polkit
# ---------------------------------------------------------------------------


def _setup_release(tmp_path):
    """Build a store + a quarantined file ready to release."""
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "doc.pdf")
    with open(qpath, "wb") as f:
        f.write(b"contents")
    row_id = store.record(quarantine_path=qpath, filename="doc.pdf",
                          source_url="https://x/doc.pdf",
                          scan_result="clean")
    return store, qpath, row_id


def test_release_authorized_moves_file(tmp_path):
    from qdbrowser.quarantine import release
    store, qpath, row_id = _setup_release(tmp_path)
    out_dir = tmp_path / "Downloads"
    result = release(store, row_id, str(out_dir), authorized=True)
    assert result is not None
    assert os.path.exists(result)
    assert not os.path.exists(qpath)
    row = store.get(row_id)
    assert row["released"] == 1
    assert row["release_path"] == result
    store.close()


def test_release_denied_leaves_file(tmp_path):
    from qdbrowser.quarantine import release
    store, qpath, row_id = _setup_release(tmp_path)
    out_dir = tmp_path / "Downloads"
    result = release(store, row_id, str(out_dir), authorized=False)
    assert result is None
    assert os.path.exists(qpath)
    row = store.get(row_id)
    assert row["released"] == 0
    store.close()


def test_release_refuses_bad_scan_result(tmp_path):
    from qdbrowser.quarantine import release
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "evil.bin")
    open(qpath, "wb").close()
    row_id = store.record(quarantine_path=qpath, filename="evil.bin",
                          source_url="https://x/", scan_result="bad")
    out_dir = tmp_path / "Downloads"
    result = release(store, row_id, str(out_dir), authorized=True)
    assert result is None
    assert os.path.exists(qpath)
    store.close()


def test_release_refuses_scanner_error(tmp_path):
    from qdbrowser.quarantine import release
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "unknown.bin")
    open(qpath, "wb").close()
    row_id = store.record(quarantine_path=qpath, filename="unknown.bin",
                          source_url="https://x/", scan_result="error")
    out_dir = tmp_path / "Downloads"
    result = release(store, row_id, str(out_dir), authorized=True)
    assert result is None
    assert os.path.exists(qpath)
    store.close()


def test_release_unknown_id(tmp_path):
    from qdbrowser.quarantine import release
    store = _make_store(tmp_path)
    assert release(store, 9999, str(tmp_path / "Downloads"),
                   authorized=True) is None
    store.close()


def test_release_clobber_protection(tmp_path):
    from qdbrowser.quarantine import release
    store, qpath, row_id = _setup_release(tmp_path)
    out_dir = tmp_path / "Downloads"
    out_dir.mkdir()
    (out_dir / "doc.pdf").write_bytes(b"existing")
    result = release(store, row_id, str(out_dir), authorized=True)
    assert result is not None
    assert os.path.basename(result) != "doc.pdf"  # got a suffix
    # Original is untouched.
    assert (out_dir / "doc.pdf").read_bytes() == b"existing"
    store.close()


def test_check_release_authorized_pkcheck_zero(monkeypatch):
    from qdbrowser import quarantine

    fake = MagicMock(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(quarantine.subprocess, "run", lambda *a, **k: fake)
    assert quarantine.check_release_authorized() is True


def test_check_release_authorized_pkcheck_nonzero(monkeypatch):
    from qdbrowser import quarantine

    fake = MagicMock(returncode=1, stdout=b"", stderr=b"")
    monkeypatch.setattr(quarantine.subprocess, "run", lambda *a, **k: fake)
    assert quarantine.check_release_authorized() is False


def test_check_release_authorized_pkcheck_missing(monkeypatch):
    from qdbrowser import quarantine

    def _boom(*a, **k):
        raise FileNotFoundError("pkcheck")

    monkeypatch.setattr(quarantine.subprocess, "run", _boom)
    assert quarantine.check_release_authorized() is False


def test_release_uses_pkcheck_when_authorized_arg_none(tmp_path, monkeypatch):
    """When ``authorized`` is None, release should consult check_release_authorized."""
    from qdbrowser import quarantine

    store, qpath, row_id = _setup_release(tmp_path)
    out_dir = tmp_path / "Downloads"

    calls = []

    def _fake_check():
        calls.append(1)
        return True

    monkeypatch.setattr(quarantine, "check_release_authorized", _fake_check)
    result = quarantine.release(store, row_id, str(out_dir), authorized=None)
    assert result is not None
    assert calls == [1]
    store.close()


# ---------------------------------------------------------------------------
# release defence-in-depth (callable without the controller)
# ---------------------------------------------------------------------------


@pytest.mark.cheat_aware(
    protects="release() refuses a forged row whose quarantine_path is a "
             "symlink, even when the symlink itself sits inside the "
             "quarantine dir (so _within_dir is satisfied) — the isfile/"
             "islink check is the only thing standing between a single "
             "approval and exfiltrating an arbitrary file the symlink "
             "points at (e.g. ~/.ssh/id_rsa)",
    severity="medium",
    cheats=[
        "drop the os.path.islink(src) clause from the refusal",
        "rely on _within_dir alone (realpath of an in-dir symlink to an "
        "in-dir target still passes, so this would slip through)",
        "make the symlink the assertion target so it 'releases' green",
    ],
    consequence="a malicious/corrupt row turns the one polkit-approved "
                "release into a copy of any file readable by the user out "
                "of the sandbox",
)
def test_release_refuses_symlink_quarantine_path(tmp_path):
    """A symlink as the quarantine_path must be refused by the
    isfile/islink guard. Crucially the symlink lives *inside* the
    quarantine dir and points at a regular file *inside* the quarantine
    dir, so _within_dir() is satisfied (realpath stays in-dir) — only the
    explicit islink() check can catch it."""
    from qdbrowser.quarantine import release
    store = _make_store(tmp_path)
    # Real target file, inside the quarantine dir.
    secret = os.path.join(store.directory, "secret.bin")
    with open(secret, "wb") as f:
        f.write(b"sensitive")
    # Symlink, also inside the quarantine dir, pointing at the target.
    link = os.path.join(store.directory, "link.bin")
    os.symlink(secret, link)
    # Sanity: the link resolves inside the dir, so _within_dir passes and
    # the only remaining defence is the islink/isfile guard.
    assert store._within_dir(link) is True
    row_id = store.record(quarantine_path=link, filename="link.bin",
                          source_url="https://x/", scan_result="clean")
    out_dir = tmp_path / "Downloads"
    result = release(store, row_id, str(out_dir), authorized=True)
    assert result is None                       # refused
    assert os.path.islink(link)                 # symlink untouched
    assert os.path.exists(secret)               # target not moved
    assert not out_dir.exists() or not any(out_dir.iterdir())
    store.close()


@pytest.mark.cheat_aware(
    protects="release() re-sanitizes the stored filename so a forged/legacy "
             "row whose filename contains path traversal lands INSIDE "
             "release_dir, not at an attacker-chosen absolute path",
    severity="medium",
    cheats=[
        "drop the _sanitize_name(row['filename']) call on the release path",
        "join the raw row filename onto release_dir",
        "assert only result is not None without checking where it landed",
    ],
    consequence="a release writes an executable into ~/.config/autostart or "
                "/etc/cron.d via ../../ traversal on a single approval",
)
def test_release_sanitizes_traversal_filename(tmp_path):
    """The quarantine_path is a legitimate in-dir file, but the stored
    `filename` is a traversal payload. release() must basename/sanitize it
    so the moved file stays inside release_dir."""
    from qdbrowser.quarantine import release
    store = _make_store(tmp_path)
    qpath = os.path.join(store.directory, "real.bin")
    with open(qpath, "wb") as f:
        f.write(b"payload")
    evil_name = "../../../etc/cron.d/evil"
    row_id = store.record(quarantine_path=qpath, filename=evil_name,
                          source_url="https://x/", scan_result="clean")
    out_dir = tmp_path / "Downloads"
    result = release(store, row_id, str(out_dir), authorized=True)
    assert result is not None                   # the move succeeds...
    # ...but lands inside release_dir, with separators stripped.
    real_out = os.path.realpath(str(out_dir))
    real_result = os.path.realpath(result)
    assert real_result == real_out or real_result.startswith(
        real_out + os.sep)
    assert os.path.basename(result) == "evil"   # _sanitize_name reduction
    # Nothing escaped to the traversal target.
    assert not os.path.exists(
        os.path.join(str(tmp_path), "etc", "cron.d", "evil"))
    store.close()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_file_sidecar_and_row(tmp_path):
    store, qpath, row_id = _setup_release(tmp_path)
    sidecar = store.write_sidecar(row_id, qpath, {"source_url": "x"})
    assert os.path.exists(qpath)
    assert os.path.exists(sidecar)

    assert store.delete(row_id) is True
    assert not os.path.exists(qpath)
    assert not os.path.exists(sidecar)
    assert store.get(row_id) is None
    store.close()


def test_delete_unknown_id_returns_false(tmp_path):
    store = _make_store(tmp_path)
    assert store.delete(9999) is False
    store.close()


def test_delete_drops_row_even_if_file_missing(tmp_path):
    store, qpath, row_id = _setup_release(tmp_path)
    os.remove(qpath)  # file already gone
    assert store.delete(row_id) is True
    assert store.get(row_id) is None
    store.close()


def test_delete_confined_to_quarantine_dir(tmp_path):
    """A forged row whose quarantine_path escapes the quarantine dir must
    not let delete() unlink an arbitrary user file; the row is still
    dropped but the outside file is untouched."""
    store = _make_store(tmp_path)
    victim = tmp_path / "important.txt"
    victim.write_text("do not delete me")
    row_id = store.record(quarantine_path=str(victim), filename="x",
                          source_url="https://x", scan_result="clean")
    assert store.delete(row_id) is True
    assert victim.exists()                  # NOT deleted
    assert store.get(row_id) is None        # row gone
    store.close()


def test_delete_keeps_row_when_unlink_fails(tmp_path, monkeypatch):
    store, qpath, row_id = _setup_release(tmp_path)

    def _boom(path):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "remove", _boom)
    assert store.delete(row_id) is False
    assert store.get(row_id) is not None    # row kept, not orphaned
    store.close()


# ---------------------------------------------------------------------------
# _sanitize_name
# ---------------------------------------------------------------------------


def test_sanitize_name_strips_directories():
    from qdbrowser.quarantine import _sanitize_name
    assert _sanitize_name("../../etc/passwd") == "passwd"
    assert _sanitize_name("/abs/path/file.txt") == "file.txt"
    # Backslashes are stripped from the *characters*, not used as a
    # separator (Linux os.path.basename only splits on '/').
    out = _sanitize_name("subdir\\file.txt")
    assert "\\" not in out
    assert out == "subdirfile.txt"


def test_sanitize_name_control_chars():
    from qdbrowser.quarantine import _sanitize_name
    # NUL byte gets dropped; non-printable chars too.
    assert "\x00" not in _sanitize_name("a\x00b.txt")
    assert "\x07" not in _sanitize_name("a\x07b.txt")


def test_sanitize_name_empty_falls_back():
    from qdbrowser.quarantine import _sanitize_name
    assert _sanitize_name("") == "download"
    assert _sanitize_name(None) == "download"
    assert _sanitize_name("   ") == "download"


def test_sanitize_name_long_input_preserved():
    from qdbrowser.quarantine import _sanitize_name
    long_name = "a" * 500 + ".bin"
    # Module doesn't truncate — just sanitize. Long but printable is fine.
    assert _sanitize_name(long_name) == long_name
