"""Tests for the builtin FilterPlugin (extension-based file filtering)."""

from __future__ import annotations

from qfileman.plugins.builtin.filter import FilterPlugin


def test_no_settings_passes_everything():
    """A freshly-constructed FilterPlugin is a no-op."""
    p = FilterPlugin()
    paths = ["/a/file.py", "/a/file.txt", "/a/README"]
    assert p.filter_files(paths) == paths


def test_include_extensions_keeps_only_listed():
    p = FilterPlugin()
    p.set_include_extensions(["py", "md"])
    paths = ["/a.py", "/b.md", "/c.txt"]
    out = p.filter_files(paths)
    assert "/c.txt" not in out
    assert "/a.py" in out and "/b.md" in out


def test_include_extensions_keeps_extensionless_files():
    """Regression: previously README/Makefile were dropped from include mode."""
    p = FilterPlugin()
    p.set_include_extensions(["py"])
    paths = ["/a.py", "/README", "/Makefile", "/b.txt"]
    out = p.filter_files(paths)
    assert "/README" in out, "extension-less files should pass the include filter"
    assert "/Makefile" in out
    assert "/a.py" in out
    assert "/b.txt" not in out


def test_exclude_extensions_drops_listed():
    p = FilterPlugin()
    p.set_exclude_extensions(["log", "tmp"])
    paths = ["/a.py", "/b.log", "/c.tmp", "/d.txt"]
    out = p.filter_files(paths)
    assert "/b.log" not in out
    assert "/c.tmp" not in out
    assert "/a.py" in out and "/d.txt" in out


def test_exclude_extensions_keeps_extensionless():
    """Exclude mode doesn't penalise files without a dot."""
    p = FilterPlugin()
    p.set_exclude_extensions(["log"])
    paths = ["/a.log", "/README"]
    out = p.filter_files(paths)
    assert "/README" in out
    assert "/a.log" not in out


def test_set_include_normalises_dots_and_case():
    p = FilterPlugin()
    p.set_include_extensions([".PY", "Md"])
    paths = ["/a.py", "/B.MD", "/c.txt"]
    out = p.filter_files(paths)
    assert "/a.py" in out
    assert "/B.MD" in out
    assert "/c.txt" not in out
