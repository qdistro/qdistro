"""Security-focused tests for the qdistro App1 integration layer.

``qfileman/qdistro_integration.py`` holds the two most security-relevant
paths in the app and previously had ZERO test coverage:

  * ``qsu_run`` — root-privilege execution via the ``qsu`` CLI. We assert
    the fail-visible behavior (return 127, no fallback exec, when ``qsu``
    is absent) and the EXACT argv shape so a payload arg that starts with
    ``-`` can never be reinterpreted as a ``qsu`` flag (it lands after
    ``--``).
  * the inbound D-Bus receiver (``on_receive`` → ``_deliver_to_pane`` →
    ``_resolve_target_dir`` / ``_extension_for_kind``) — an untrusted peer
    app can push a ``kind`` + ``payload`` that gets written to disk. We
    assert the write stays inside the active pane directory and that a
    hostile ``kind`` cannot steer the auto-generated filename into a
    path-traversing name.

No network, no real D-Bus, no real ``qsu`` is touched. ``subprocess.call``
is patched the same way the plugin suites patch their shell-outs, and the
window is a plain stub object rather than a real Qt window.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from qfileman import qdistro_integration as qi

# ---------------------------------------------------------------------------
# qsu_run — fail-visible when qsu is missing
# ---------------------------------------------------------------------------

def test_qsu_run_returns_127_when_qsu_missing(monkeypatch):
    """No qsu on $PATH → return 127 and NEVER spawn anything.

    This is the fail-visible contract: the caller is told "cannot elevate"
    rather than the op silently running unprivileged or, worse, some
    fallback binary getting executed in qsu's place.
    """
    monkeypatch.setattr(qi.shutil, "which", lambda _n: None)

    # Guard: if anything tries to spawn, fail loudly instead of silently
    # passing through a stub.
    def _boom(*a, **k):  # pragma: no cover - only runs if the test breaks
        raise AssertionError("subprocess.call must NOT run when qsu is absent")

    monkeypatch.setattr(qi.subprocess, "call", _boom)

    assert qi.qsu_run(["chmod", "0644", "/etc/hosts"]) == 127


@pytest.mark.cheat_aware(
    protects="payload argv is passed to qsu positionally AFTER `--`, so an "
    "arg beginning with `-` cannot be parsed as a qsu flag; user is the "
    "value of `-u`, and args are stringified",
    severity="critical",
    cheats=[
        "assert only the prefix (['qsu','-u',...]) and ignore arg placement",
        "use `in cmd` membership checks instead of exact list equality",
        "drop the malicious leading-dash arg from the expected list",
        "stub qsu_run / shutil.which so the real argv is never built",
    ],
    consequence="a privileged op's argument like `--no-preserve-root` or "
    "`-rf` could be swallowed as a qsu option, changing what runs as root",
)
def test_qsu_run_argv_is_exact_and_dash_args_are_after_double_dash(monkeypatch):
    """Assert the EXACT argv list, including a hostile leading-dash arg."""
    monkeypatch.setattr(qi.shutil, "which", lambda _n: "/usr/bin/qsu")

    captured: dict = {}

    def fake_call(cmd, *a, **k):
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(qi.subprocess, "call", fake_call)

    # Note the leading-dash arg and a non-str arg: both must survive intact,
    # stringified, and sit strictly after the `--` separator.
    payload_argv = ["rm", "-rf", "--no-preserve-root", 7, "/srv/data"]
    rc = qi.qsu_run(payload_argv, target_user="root")

    assert rc == 0
    assert captured["cmd"] == [
        "qsu", "-u", "root", "--",
        "rm", "-rf", "--no-preserve-root", "7", "/srv/data",
    ]

    # Structural belt-and-suspenders: every payload arg is positioned after
    # the lone `--` separator, never before it.
    cmd = captured["cmd"]
    sep = cmd.index("--")
    assert cmd.count("--") == 1
    assert all(cmd[sep + 1 + i] == str(payload_argv[i])
               for i in range(len(payload_argv)))


def test_qsu_run_honors_target_user(monkeypatch):
    """The target user is the value of `-u`, stringified, not a payload arg."""
    monkeypatch.setattr(qi.shutil, "which", lambda _n: "/usr/bin/qsu")
    captured: dict = {}
    monkeypatch.setattr(qi.subprocess, "call",
                        lambda cmd, *a, **k: captured.setdefault("cmd", cmd) or 0)

    qi.qsu_run(["id"], target_user="postgres")
    assert captured["cmd"] == ["qsu", "-u", "postgres", "--", "id"]


def test_qsu_run_returns_126_on_oserror(monkeypatch):
    """A spawn-time OSError is reported distinctly (126), not as success."""
    monkeypatch.setattr(qi.shutil, "which", lambda _n: "/usr/bin/qsu")

    def fake_call(cmd, *a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(qi.subprocess, "call", fake_call)
    assert qi.qsu_run(["true"]) == 126


def test_qsu_run_propagates_exit_code(monkeypatch):
    """The spawned command's own exit code is returned verbatim."""
    monkeypatch.setattr(qi.shutil, "which", lambda _n: "/usr/bin/qsu")
    monkeypatch.setattr(qi.subprocess, "call", lambda cmd, *a, **k: 42)
    assert qi.qsu_run(["false"]) == 42


# ---------------------------------------------------------------------------
# _extension_for_kind — hostile kind cannot inject a traversing filename
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="a hostile inbound `kind` only ever yields a fixed, safe file "
    "extension (one of the mapping values or '.txt'/'.bin') and can never "
    "introduce a path separator or traversal into the auto-named file",
    severity="high",
    cheats=[
        "only test the benign 'text/plain' → '.txt' happy path",
        "assert `endswith` instead of checking for '/', '\\\\', '..'",
    ],
    consequence="a peer app's Send-To could steer the saved-file name into "
    "'../../...' and write outside the pane directory as the file manager",
)
@pytest.mark.parametrize("hostile_kind", [
    "../../../etc/cron.d/evil",
    "text/../../../../etc/passwd",
    "application/octet-stream/../../foo",
    "..%2f..%2fetc",
    "text/plain\x00.sh",
    "/absolute/path",
    "C:\\Windows\\system32",
    None,
])
def test_extension_for_kind_never_yields_traversal(hostile_kind):
    """No hostile kind escapes the fixed extension set."""
    ext = qi._extension_for_kind(hostile_kind)
    # The extension is consumed as `f"...{ext}"` in a single path segment.
    assert "/" not in ext
    assert "\\" not in ext
    assert ".." not in ext
    assert "\x00" not in ext
    assert ext.startswith(".")
    # It is always one of the known-safe outputs.
    assert ext in {".txt", ".md", ".html", ".json", ".bin"}


def test_extension_for_kind_known_mappings():
    """The documented mapping is honored, case-insensitively."""
    assert qi._extension_for_kind("text/plain") == ".txt"
    assert qi._extension_for_kind("TEXT/PLAIN") == ".txt"
    assert qi._extension_for_kind("text/markdown") == ".md"
    assert qi._extension_for_kind("application/json") == ".json"
    assert qi._extension_for_kind("application/octet-stream") == ".bin"
    # Unknown text/* falls back to .txt; everything else to .bin.
    assert qi._extension_for_kind("text/x-weird") == ".txt"
    assert qi._extension_for_kind("image/png") == ".bin"


# ---------------------------------------------------------------------------
# _resolve_target_dir — picks the active pane dir, with fallbacks
# ---------------------------------------------------------------------------

def test_resolve_target_dir_prefers_active_pane(tmp_path):
    pane = SimpleNamespace(current_path=str(tmp_path))
    window = SimpleNamespace(_active_pane=pane)
    assert qi._resolve_target_dir(window) == tmp_path


def test_resolve_target_dir_falls_back_to_window_path(tmp_path):
    window = SimpleNamespace(_current_path=str(tmp_path))
    assert qi._resolve_target_dir(window) == tmp_path


def test_resolve_target_dir_falls_back_to_home(monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    window = SimpleNamespace()  # no pane, no path attrs
    assert qi._resolve_target_dir(window) == fake_home


# ---------------------------------------------------------------------------
# _deliver_to_pane — the inbound write stays inside the pane directory
# ---------------------------------------------------------------------------

@pytest.mark.cheat_aware(
    protects="an untrusted inbound payload is written as a single file "
    "STRICTLY inside the active pane directory; neither the `kind` nor the "
    "`payload` can redirect the write outside that directory",
    severity="high",
    cheats=[
        "only assert the file content, never its resolved location",
        "use the payload itself as the expected filename",
        "skip the parent-directory containment check",
    ],
    consequence="a peer app could write arbitrary files outside the pane "
    "(e.g. into autostart/cron dirs) through the file manager's privileges",
)
@pytest.mark.parametrize("hostile_kind", [
    "../../../etc/evil",
    "text/../../escape",
    "application/octet-stream",
    "text/plain",
])
def test_deliver_to_pane_write_stays_inside_pane_dir(tmp_path, hostile_kind):
    pane_dir = tmp_path / "pane"
    pane_dir.mkdir()
    pane = SimpleNamespace(current_path=str(pane_dir))
    window = SimpleNamespace(_active_pane=pane)

    payload = "../../../../etc/passwd\n:root:0:0:"  # traversal-y CONTENT
    qi._deliver_to_pane(window, hostile_kind, payload)

    written = list(pane_dir.iterdir())
    assert len(written) == 1, "exactly one file should be created"
    f = written[0]

    # Containment: the resolved file is a direct child of the pane dir, and
    # nothing was written above it (no traversal escaped).
    assert f.resolve().parent == pane_dir.resolve()
    assert f.parent == pane_dir
    assert f.name.startswith("qdistro-recv-")
    assert ".." not in f.name and "/" not in f.name
    # The payload is the file's CONTENT, never part of its name.
    assert f.read_text(encoding="utf-8") == payload
    assert payload not in f.name


def test_deliver_to_pane_failure_is_swallowed_to_stderr(capsys):
    """A delivery error degrades gracefully (printed, not raised)."""
    # _resolve_target_dir returns $HOME for a bare window, but write_text on a
    # Path built from a window whose path is a *file* will raise; simplest is a
    # window whose pane path points at a non-writable/nonexistent location.
    window = SimpleNamespace(
        _active_pane=SimpleNamespace(
            current_path="/proc/nonexistent-qfileman-test-dir"))
    # Must not raise.
    qi._deliver_to_pane(window, "text/plain", "hi")
    err = capsys.readouterr().err
    assert "deliver failed" in err


# ---------------------------------------------------------------------------
# No-op degradation when the qdistro_app SDK / D-Bus is unavailable
# ---------------------------------------------------------------------------

def test_maybe_install_noop_without_sdk(monkeypatch, capsys):
    """No SDK importable → registration is skipped, returns None, warns."""
    monkeypatch.setattr(qi, "_app_receiver", None)
    result = qi.maybe_install(SimpleNamespace())
    assert result is None
    assert "registration skipped" in capsys.readouterr().err


def test_maybe_install_returns_none_when_register_yields_none(monkeypatch):
    """SDK present but register_app returns None (bus unreachable) → None."""
    fake_sdk = SimpleNamespace(register_app=lambda *a, **k: None)
    monkeypatch.setattr(qi, "_app_receiver", fake_sdk)
    assert qi.maybe_install(SimpleNamespace()) is None


def test_send_to_targets_noop_without_sdk(monkeypatch):
    monkeypatch.setattr(qi, "_app_receiver", None)
    assert qi.send_to_targets() == []


def test_send_payload_noop_without_sdk(monkeypatch):
    monkeypatch.setattr(qi, "_app_receiver", None)
    assert qi.send_payload(0, "org.qdistro.X.uid0", "hi") is False
