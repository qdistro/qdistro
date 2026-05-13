"""Tests for silo_colors.chip_for_uid."""
from __future__ import annotations

from pathlib import Path

import pytest

from silo_colors import chip_for_uid, reset_cache, _PALETTE


@pytest.fixture(autouse=True)
def _reset():
    """Each test starts with a fresh cache so override-path swaps work."""
    reset_cache()
    yield
    reset_cache()


def test_deterministic_for_same_uid(tmp_path):
    missing = tmp_path / "no-such.toml"
    a = chip_for_uid(2000, override_path=missing)
    b = chip_for_uid(2000, override_path=missing)
    assert a == b
    assert a in _PALETTE


def test_mostly_different_across_neighbors(tmp_path):
    """Not a contractual guarantee — palette is size 10 so collisions
    happen — but hashing 20 adjacent uids should cover most of the
    palette. If this drops below 6 distinct values, the hash is too
    skewed and deserves attention."""
    missing = tmp_path / "no-such.toml"
    colors = {chip_for_uid(u, override_path=missing) for u in range(2000, 2020)}
    assert len(colors) >= 6


def test_override_file_wins(tmp_path):
    override = tmp_path / "silo-colors.toml"
    override.write_text('"2000" = "red"\n"3000" = "magenta"\n')
    assert chip_for_uid(2000, override_path=override) == "red"
    assert chip_for_uid(3000, override_path=override) == "magenta"
    # Uncovered uid still falls through to hash.
    assert chip_for_uid(4000, override_path=override) in _PALETTE


def test_override_cache_keyed_by_path(tmp_path):
    one = tmp_path / "a.toml"; one.write_text('"2000" = "red"\n')
    two = tmp_path / "b.toml"; two.write_text('"2000" = "blue"\n')
    assert chip_for_uid(2000, override_path=one) == "red"
    assert chip_for_uid(2000, override_path=two) == "blue"


def test_missing_override_file_is_silent(tmp_path):
    gone = tmp_path / "missing.toml"
    # Should not raise, falls through to hash.
    c = chip_for_uid(2000, override_path=gone)
    assert c in _PALETTE


def test_malformed_override_falls_through(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text('this is = not valid TOML at all [')
    # Should not raise; prints a warning once.
    c = chip_for_uid(2000, override_path=bad)
    assert c in _PALETTE
    out = capsys.readouterr()
    assert "silo_colors" in (out.out + out.err)


def test_override_non_integer_key_skipped(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('"not-a-number" = "red"\n"2000" = "green"\n')
    assert chip_for_uid(2000, override_path=bad) == "green"


def test_override_non_string_value_skipped(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('"2000" = 42\n"3000" = "cyan"\n')
    assert chip_for_uid(3000, override_path=bad) == "cyan"
    # 2000 falls through to hash (42 isn't a valid color name, skipped).
    assert chip_for_uid(2000, override_path=bad) in _PALETTE
