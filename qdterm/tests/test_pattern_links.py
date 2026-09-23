"""Tests for the pattern_links URL handler plugins.

Focus: the StackTraceHandler must not open paths parsed from (possibly
hostile) terminal output unless they refer to an existing regular file on
disk -- mirroring the existence guard that FilePathHandler already applies.
"""

import os

import pytest
from qterminator.plugins import pattern_links
from qterminator.plugins.pattern_links import FilePathHandler, StackTraceHandler


@pytest.fixture
def spawned(monkeypatch):
    """Capture subprocess.Popen argv lists instead of launching processes."""
    calls = []

    class FakePopen:
        def __init__(self, argv, *args, **kwargs):
            calls.append(list(argv))

    monkeypatch.setattr(pattern_links.subprocess, "Popen", FakePopen)
    return calls


# ---------------------------------------------------------------------------
# StackTraceHandler existence guard
# ---------------------------------------------------------------------------

def test_stacktrace_opens_existing_regular_file(spawned, tmp_path, monkeypatch):
    f = tmp_path / "boom.py"
    f.write_text("x = 1\n")
    monkeypatch.delenv("EDITOR", raising=False)

    h = StackTraceHandler()
    result = h.handle_url(f'File "{f}", line 42')

    assert result == f"{f}:42"
    assert spawned, "expected an open for an existing regular file"
    assert str(f) in spawned[-1]


def test_stacktrace_rejects_nonexistent_path(spawned, tmp_path):
    missing = tmp_path / "does_not_exist.py"
    h = StackTraceHandler()
    result = h.handle_url(f'File "{missing}", line 7')

    assert result is None
    assert spawned == [], "must not open a path that does not exist on disk"


def test_stacktrace_rejects_directory(spawned, tmp_path):
    # A directory exists but is not a regular file.
    d = tmp_path / "subdir.py"  # extension irrelevant; it's a directory
    d.mkdir()
    h = StackTraceHandler()
    result = h.handle_url(f'File "{d}", line 1')

    assert result is None
    assert spawned == [], "must not open a directory"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo")
def test_stacktrace_rejects_fifo(spawned, tmp_path):
    fifo = tmp_path / "pipe.py"
    os.mkfifo(fifo)
    h = StackTraceHandler()
    result = h.handle_url(f'File "{fifo}", line 1')

    assert result is None
    assert spawned == [], "must not open a non-regular special file (FIFO)"


def test_stacktrace_java_pattern_guarded(spawned, tmp_path):
    # Java pattern uses a bare filename with no path; it won't exist in CWD.
    h = StackTraceHandler()
    result = h.handle_url("at com.example.Main(Attacker.java:13)")

    assert result is None
    assert spawned == []


def test_stacktrace_go_generic_pattern_guarded(spawned, tmp_path):
    missing = tmp_path / "evil.go"
    h = StackTraceHandler()
    result = h.handle_url(f"{missing}:99")

    assert result is None
    assert spawned == []


def test_stacktrace_uses_editor_when_set(spawned, tmp_path, monkeypatch):
    f = tmp_path / "trace.py"
    f.write_text("\n")
    monkeypatch.setenv("EDITOR", "myeditor")

    h = StackTraceHandler()
    result = h.handle_url(f'File "{f}", line 5')

    assert result == f"{f}:5"
    assert spawned, "expected editor launch"
    argv = spawned[-1]
    assert argv[0] == "myeditor"
    assert "+5" in argv
    assert str(f) in argv


# ---------------------------------------------------------------------------
# FilePathHandler parity (existing guard, asserted for regression safety)
# ---------------------------------------------------------------------------

def test_filepath_rejects_nonexistent(spawned, tmp_path):
    missing = tmp_path / "nope"
    h = FilePathHandler()
    assert h.handle_url(str(missing)) is None
    assert spawned == []


def test_filepath_opens_existing(spawned, tmp_path):
    f = tmp_path / "real.txt"
    f.write_text("hi\n")
    h = FilePathHandler()
    assert h.handle_url(str(f)) == str(f)
    assert spawned and str(f) in spawned[-1]
