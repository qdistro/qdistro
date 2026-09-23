from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


HELPER = (
    Path(__file__).resolve().parents[1]
    / "Scripts"
    / "python"
    / "src"
    / "plugins"
    / "plugin-helper.py"
)


spec = importlib.util.spec_from_file_location("plugin_helper", HELPER)
plugin_helper = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(plugin_helper)


@pytest.mark.parametrize("plugin_id", ["safe", "safe.name", "safe-name_1"])
def test_plugin_id_accepts_safe_names(plugin_id):
    plugin_helper._validate_plugin_id(plugin_id)


@pytest.mark.parametrize("plugin_id", [
    "../x",
    ".",
    "..",
    "--stdin",
    "-rf",
    "x/y",
    "x;y",
    "x y",
    "",
    # F6: the charset regex alone admits these — a ".." traversal segment
    # embedded in an otherwise-valid id, and all-dot ids — so they must be
    # rejected to match PluginRegistry.isSafePluginId in QML.
    "safe..x",
    "a..b",
    "...",
])
def test_plugin_id_rejects_path_and_shell_syntax(plugin_id):
    with pytest.raises(ValueError):
        plugin_helper._validate_plugin_id(plugin_id)


@pytest.mark.parametrize("key", ["abc123:safe..x", "ab..cd", "..", "abcdef:..."])
def test_composite_key_rejects_embedded_traversal(key):
    # F6: composite key suffix must also reject embedded ".." / all-dot.
    with pytest.raises(ValueError):
        plugin_helper._validate_composite_key(key)


@pytest.mark.parametrize("url", [
    "https://example.test/repo.git",
    "ssh://git@example.test/repo.git",
])
def test_repo_url_accepts_https_and_ssh(url):
    plugin_helper._validate_repo_url(url)


@pytest.mark.parametrize("url", [
    "",
    "not a url",
    "javascript:alert(1)",
    "https:///repo",
    "data:text/plain,repo",
    "blob:https://example.test/abc",
    "git@example.test:repo\nx",
    # F9: these transports are no longer accepted (only https/ssh).
    "git@example.test:repo.git",       # scp-style shorthand
    "http://example.test/repo.git",    # cleartext
    "git://example.test/repo.git",     # unauthenticated
    "file:///tmp/repo",                # local path → cross-silo staging
    "file:relative/repo",
])
def test_repo_url_rejects_unsafe_shapes(url):
    with pytest.raises(ValueError):
        plugin_helper._validate_repo_url(url)


def test_plugin_service_no_longer_uses_shell_for_registry_or_install():
    service = (Path(__file__).resolve().parents[1] / "Services" / "Qdshell" / "PluginService.qml").read_text(encoding="utf-8")
    assert 'command: ["sh", "-c"' not in service
    assert "plugin-helper.py" in service


def test_install_plugin_preserves_existing_settings(tmp_path, monkeypatch):
    dest = tmp_path / "plugins" / "safe"
    dest.mkdir(parents=True)
    (dest / "settings.json").write_text('{"keep": true}', encoding="utf-8")
    (dest / "old.txt").write_text("old", encoding="utf-8")

    def fake_clone_sparse(repo_url, plugin_id, temp_dir):
        src = temp_dir / plugin_id
        src.mkdir()
        (src / "manifest.json").write_text('{"id":"safe"}', encoding="utf-8")
        (src / "old.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(plugin_helper, "_clone_sparse", fake_clone_sparse)

    assert plugin_helper.install_plugin("https://example.test/repo.git", "safe", str(dest)) == 0
    assert (dest / "settings.json").read_text(encoding="utf-8") == '{"keep": true}'
    assert (dest / "old.txt").read_text(encoding="utf-8") == "new"
    assert (dest / "manifest.json").is_file()


def test_clone_sparse_uses_end_of_options_separator(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, cwd=None):
        calls.append((argv, cwd))

    monkeypatch.setattr(plugin_helper, "_run", fake_run)
    plugin_helper._clone_sparse("https://example.test/repo.git", "safe", tmp_path)

    assert calls[0][0][-3:] == ["--", "https://example.test/repo.git", str(tmp_path)]
    assert calls[1][0][-2:] == ["--", "safe"]
