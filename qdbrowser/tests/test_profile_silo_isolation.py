"""J8 — cross-silo persistent-profile isolation.

The browser is a per-silo app: two qdbrowser instances launched in different
silos share ``$HOME``, so the persistent profile's on-disk storage must be
keyed on the silo (``$QDISTRO_SILO``) — otherwise both read/write the same
cookie jar / cache / localStorage and the silo web-identity boundary is
defeated (opus HIGH #7 / J8).

These tests pin:
  - two distinct silos → distinct persistentStoragePath AND cachePath;
  - no silo set → the legacy flat path (no migration for standalone use);
  - the off-the-record "private" profile is silo-independent (no storage);
  - ``profile_silo_segment`` is env-only + sanitized (unlike ``current_silo``,
    it does NOT fall back to the username, which cannot distinguish silos).
"""
from __future__ import annotations

import pytest
import qdbrowser.webview as wv_mod
from qdbrowser.clipboard_silo import profile_silo_segment
from qdbrowser.webview import get_profile


def _storage_for(silo, name, monkeypatch, tmp_path):
    """Create a fresh profile for (silo, name) under a temp $HOME and return
    (persistentStoragePath, cachePath)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    if silo is None:
        monkeypatch.delenv("QDISTRO_SILO", raising=False)
    else:
        monkeypatch.setenv("QDISTRO_SILO", silo)
    wv_mod._PROFILES.clear()
    prof = get_profile(name)
    return prof.persistentStoragePath(), prof.cachePath()


def test_distinct_silos_get_distinct_persistent_storage(qapp, tmp_path, monkeypatch):
    a_store, a_cache = _storage_for("alpha", "default", monkeypatch, tmp_path)
    b_store, b_cache = _storage_for("beta", "default", monkeypatch, tmp_path)
    try:
        assert a_store != b_store
        assert a_cache != b_cache
        # Each path is nested under its own silo segment.
        assert "/profiles/alpha/default/" in a_store + "/"
        assert "/profiles/beta/default/" in b_store + "/"
        assert "alpha" not in b_store and "beta" not in a_store
    finally:
        wv_mod._PROFILES.clear()


def test_no_silo_uses_legacy_flat_path(qapp, tmp_path, monkeypatch):
    store, cache = _storage_for(None, "default", monkeypatch, tmp_path)
    try:
        # No silo segment — unchanged from pre-J8 so existing profiles are
        # not orphaned on upgrade.
        assert store.endswith("/profiles/default/storage")
        assert cache.endswith("/profiles/default/cache")
    finally:
        wv_mod._PROFILES.clear()


def test_same_silo_and_name_share_one_profile(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QDISTRO_SILO", "work")
    wv_mod._PROFILES.clear()
    try:
        p1 = get_profile("default")
        p2 = get_profile("default")
        assert p1 is p2  # cached within the silo
    finally:
        wv_mod._PROFILES.clear()


def test_private_profile_is_off_the_record_and_silo_independent(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QDISTRO_SILO", "alpha")
    wv_mod._PROFILES.clear()
    try:
        prof = get_profile("private")
        # Off-the-record is the real guarantee: nothing is written to disk,
        # so there is no per-silo cookie jar to leak. (Qt still reports a
        # default storage path string for OTR profiles; it is never used.)
        assert prof.isOffTheRecord()
        # Our code never nests the private profile under the silo.
        assert "/profiles/alpha/" not in prof.persistentStoragePath()
    finally:
        wv_mod._PROFILES.clear()


@pytest.mark.parametrize("bad_name", [
    "../beta/default",       # climb out of <silo>/ into a sibling silo
    "../../../.mozilla/x",   # escape the qdbrowser tree entirely
    "beta/default",          # embedded separator → collision with silo beta
    "a\\b",                  # backslash separator
    "..",                    # parent
    ".",                     # cwd
    "",                      # empty
])
def test_unsafe_profile_names_are_rejected(qapp, tmp_path, monkeypatch, bad_name):
    """A crafted profile ``name`` (reachable via ``--profile`` and the agent
    RPC ``open_tab(profile=...)``) must not be able to traverse out of the
    per-silo segment. ``get_profile`` fails closed rather than building a path
    that escapes ``profiles/<silo>/``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QDISTRO_SILO", "alpha")
    wv_mod._PROFILES.clear()
    try:
        with pytest.raises(ValueError):
            get_profile(bad_name)
        # Nothing was cached and no sibling-silo dir was created.
        assert not wv_mod._PROFILES
        assert not (tmp_path / ".local/share/qdbrowser/profiles/beta").exists()
    finally:
        wv_mod._PROFILES.clear()


def test_no_silo_named_profile_cannot_collide_with_a_silo_dir(qapp, tmp_path, monkeypatch):
    """Without a silo set, a name like ``beta/default`` used to resolve to the
    same on-disk path as ``QDISTRO_SILO=beta`` + profile ``default``. The
    basename check now rejects it, closing that cross-silo collision."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("QDISTRO_SILO", raising=False)
    wv_mod._PROFILES.clear()
    try:
        with pytest.raises(ValueError):
            get_profile("beta/default")
    finally:
        wv_mod._PROFILES.clear()


def test_profile_silo_segment_is_env_only_and_sanitized(monkeypatch):
    monkeypatch.setenv("QDISTRO_SILO", "work")
    assert profile_silo_segment() == "work"
    # Out-of-grammar values are rejected (no path traversal / markup).
    monkeypatch.setenv("QDISTRO_SILO", "../../etc")
    assert profile_silo_segment() == ""
    # Unlike current_silo(), NO username fallback: unset => "".
    monkeypatch.delenv("QDISTRO_SILO", raising=False)
    assert profile_silo_segment() == ""
