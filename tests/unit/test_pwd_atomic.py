"""Unit tests for the hardened pwd atomic JSON writer.

Pins the security-relevant behavior that motivated consolidating the per-module
`_atomic_write` copies: the secret state file is created 0600 *from the start*
(no umask-default window), the replace is atomic, a failed write leaves no temp
and does not clobber an existing file, and over-broad modes are rejected.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

import qdistro_pwd_atomic as a


def test_round_trips_content(tmp_path):
    path = str(tmp_path / "vault.json")
    body = {"b": 2, "a": 1, "items": []}
    a.atomic_write_json(path, body)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == body
    # pretty-printed + key-sorted (matches the writers it replaced)
    text = open(path, encoding="utf-8").read()
    assert text.index('"a"') < text.index('"b"')
    assert "\n" in text


def test_final_file_is_0600_even_under_permissive_umask(tmp_path):
    path = str(tmp_path / "secret.json")
    old = os.umask(0)
    try:
        a.atomic_write_json(path, {"k": "v"})
    finally:
        os.umask(old)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"{mode:#o}"


def test_temp_is_never_group_or_other_readable(tmp_path, monkeypatch):
    """The temp must be 0600 from creation through the rename — the bug we fixed
    was create-at-umask-then-chmod, which exposes the secret briefly."""
    path = str(tmp_path / "secret.json")

    # 1. mkstemp must create the temp 0600 from the start (even under umask 0).
    created_modes = []
    real_mkstemp = a.tempfile.mkstemp

    def mkstemp_spy(*args, **kwargs):
        fd, tmp = real_mkstemp(*args, **kwargs)
        created_modes.append(stat.S_IMODE(os.stat(tmp).st_mode))
        return fd, tmp

    # 2. at rename time the temp must still be exactly 0600 (no widening).
    replace_modes = []
    real_replace = a.os.replace

    def replace_spy(src, dst):
        replace_modes.append(stat.S_IMODE(os.stat(src).st_mode))
        return real_replace(src, dst)

    monkeypatch.setattr(a.tempfile, "mkstemp", mkstemp_spy)
    monkeypatch.setattr(a.os, "replace", replace_spy)

    old = os.umask(0)
    try:
        a.atomic_write_json(path, {"k": "v"})
    finally:
        os.umask(old)

    assert created_modes and all(m == 0o600 for m in created_modes), created_modes
    assert replace_modes == [0o600], replace_modes


def test_rejects_mode_broader_than_0600(tmp_path):
    path = str(tmp_path / "x.json")
    with pytest.raises(ValueError):
        a.atomic_write_json(path, {"k": "v"}, mode=0o644)
    assert not os.path.exists(path)


def test_failed_write_leaves_no_temp_and_keeps_old_file(tmp_path):
    path = str(tmp_path / "vault.json")
    a.atomic_write_json(path, {"version": 1})
    before = open(path, encoding="utf-8").read()

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        a.atomic_write_json(path, {"bad": Unserializable()})

    # old file intact, no stray temp files left behind
    assert open(path, encoding="utf-8").read() == before
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith("vault.json.")]
    assert leftovers == [], leftovers


def test_no_fd_leak_when_setup_fails_before_write(tmp_path, monkeypatch):
    """If fchmod/fdopen raise before the file object takes ownership, the raw
    mkstemp fd must still be closed (no leak) and the temp removed."""
    path = str(tmp_path / "vault.json")
    captured = {}
    real_mkstemp = a.tempfile.mkstemp

    def mkstemp_spy(*args, **kwargs):
        fd, tmp = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        return fd, tmp

    def boom(_fd, _mode):
        raise OSError("simulated fchmod failure")

    monkeypatch.setattr(a.tempfile, "mkstemp", mkstemp_spy)
    monkeypatch.setattr(a.os, "fchmod", boom)

    with pytest.raises(OSError, match="simulated fchmod failure"):
        a.atomic_write_json(path, {"k": "v"})

    # the fd must be closed (fstat on a closed fd raises EBADF)
    with pytest.raises(OSError):
        os.fstat(captured["fd"])
    # no temp left behind, no destination created
    assert not os.path.exists(path)
    assert [p for p in os.listdir(tmp_path) if p.startswith("vault.json.")] == []


def test_atomic_replace_over_existing(tmp_path):
    path = str(tmp_path / "vault.json")
    a.atomic_write_json(path, {"n": 1})
    a.atomic_write_json(path, {"n": 2})
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"n": 2}
