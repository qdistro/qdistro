"""F3: confined, symlink-safe theming output writes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

THEMING = (
    Path(__file__).resolve().parents[1]
    / "Scripts" / "python" / "src" / "theming"
)
sys.path.insert(0, str(THEMING))

from lib.safe_write import (  # noqa: E402
    OutputConfinementError,
    confine_output_path,
    safe_write_text,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".cache").mkdir(parents=True)
    (home / ".local" / "share").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # safe_write resolves ~ via os.path.expanduser, which honours $HOME.
    monkeypatch.delenv("USERPROFILE", raising=False)
    return home


def test_allows_known_theme_targets(fake_home):
    for rel in [
        ".config/kitty/themes/qdshell.conf",
        ".cache/wal/colors.json",
        ".local/share/color-schemes/qdshell.colors",
        # Built-in TemplateRegistry targets outside the XDG roots.
        ".vscode/extensions/qdshell.qdshell-theme/themes/QdshellTheme-color-theme.json",
        ".vscode-oss/extensions/qdshell.qdshell-theme/themes/x.json",
        ".emacs.d/qdshell-theme.el",
    ]:
        resolved = confine_output_path(fake_home / rel)
        assert resolved == (fake_home / rel).resolve()


@pytest.mark.parametrize("rel", [
    ".bashrc",
    ".zshrc",
    ".profile",
    ".config/autostart/x.desktop",
    ".config/systemd/user/evil.service",
    ".config/environment.d/x.conf",
    ".config/fish/config.fish",
    ".ssh/authorized_keys",
    ".gnupg/gpg.conf",
    ".local/bin/x",
])
def test_rejects_persistence_and_credential_targets(fake_home, rel):
    with pytest.raises(OutputConfinementError):
        confine_output_path(fake_home / rel)


def test_rejects_outside_home(fake_home):
    with pytest.raises(OutputConfinementError):
        confine_output_path("/etc/passwd")
    with pytest.raises(OutputConfinementError):
        confine_output_path("/tmp/x")


def test_rejects_traversal_escape(fake_home):
    # ../../etc/passwd from inside .config must resolve out and be rejected.
    with pytest.raises(OutputConfinementError):
        confine_output_path(fake_home / ".config" / ".." / ".." / "etc" / "passwd")


def test_rejects_symlinked_parent_escaping(fake_home):
    # A symlinked parent that points outside the allowed roots must be rejected,
    # because resolve() collapses it to its real (out-of-bounds) location.
    outside = fake_home.parent / "outside"
    outside.mkdir()
    link = fake_home / ".config" / "kitty"
    link.symlink_to(outside)
    with pytest.raises(OutputConfinementError):
        confine_output_path(link / "themes" / "qdshell.conf")


def test_safe_write_creates_and_writes(fake_home):
    target = fake_home / ".config" / "kitty" / "themes" / "qdshell.conf"
    safe_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_safe_write_refuses_symlinked_final_target(fake_home):
    # A pre-planted symlink AT the output path must not redirect the write
    # (O_NOFOLLOW => OSError). Point it at a sibling allowed file so the failure
    # is the symlink itself, not confinement.
    victim = fake_home / ".config" / "victim"
    victim.write_text("original")
    link = fake_home / ".config" / "app" / "theme.conf"
    link.parent.mkdir(parents=True)
    link.symlink_to(victim)
    with pytest.raises(OSError):
        safe_write_text(link, "overwritten")
    assert victim.read_text() == "original"


def test_safe_write_refuses_unsafe_path(fake_home):
    with pytest.raises(OutputConfinementError):
        safe_write_text(fake_home / ".bashrc", "evil")
