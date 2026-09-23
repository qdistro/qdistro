from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook import plugins as plugins_mod
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear()
    s.sync()


def test_discover_finds_builtin_plugins(tmp_notebook: Path):
    infos = plugins_mod.discover(tmp_notebook)
    keys = {i.key for i in infos}
    assert "journal_plugin" in keys
    assert "linkmap_plugin" in keys
    assert "word_of_the_day" in keys


def test_user_plugin_loaded_from_notebook(tmp_path: Path):
    root = tmp_path / "nb"
    root.mkdir()
    plugdir = root / ".qnotebook" / "plugins"
    plugdir.mkdir(parents=True)
    (plugdir / "myplug.py").write_text(
        "class Plugin:\n"
        "    name = 'My Plug'\n"
        "    description = 'test'\n"
        "    def setup(self, w):\n"
        "        w._myplug_setup_called = True\n",
        encoding="utf-8",
    )
    infos = plugins_mod.discover(root)
    user = [i for i in infos if i.source == "user"]
    assert any(i.name == "My Plug" for i in user)
    assert all(i.plugin is None for i in user)


def test_user_plugin_discovery_does_not_execute_code(tmp_path: Path):
    root = tmp_path / "nb"
    root.mkdir()
    marker = tmp_path / "executed"
    plugdir = root / ".qnotebook" / "plugins"
    plugdir.mkdir(parents=True)
    (plugdir / "side_effect.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "class Plugin:\n"
        "    name = 'Side Effect'\n"
        "    description = 'must not run on discover'\n"
        "    def setup(self, w):\n"
        "        w.side_effect = True\n",
        encoding="utf-8",
    )
    infos = plugins_mod.discover(root)
    assert any(i.name == "Side Effect" for i in infos)
    assert not marker.exists()


def test_enabled_user_plugin_executes_at_setup_only(tmp_path: Path):
    root = tmp_path / "nb"
    root.mkdir()
    marker = tmp_path / "executed"
    plugdir = root / ".qnotebook" / "plugins"
    plugdir.mkdir(parents=True)
    (plugdir / "side_effect.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class Plugin:\n"
        "    name = 'Side Effect'\n"
        "    description = 'runs only when enabled'\n"
        "    def setup(self, w):\n"
        "        w.side_effect = True\n",
        encoding="utf-8",
    )
    infos = plugins_mod.discover(root)
    assert not marker.exists()
    window = type("Window", (), {})()
    info = next(i for i in infos if i.key == "user:side_effect")
    token = plugins_mod.enabled_token(info)
    activated = plugins_mod.setup_enabled(window, infos, {token})
    assert activated == ["user:side_effect"]
    assert marker.read_text(encoding="utf-8") == "imported"
    assert window.side_effect is True


def test_window_plugins_menu_populated(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    assert hasattr(w, "m_plugins")
    actions = [a.text() for a in w.m_plugins.actions()]
    assert any("Journal" in t for t in actions)


def test_enabling_plugin_persists(qapp, tmp_notebook: Path, qtbot):
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    w._toggle_plugin("word_of_the_day", True)
    s = QSettings("qnotebook", "qnotebook")
    enabled = s.value("plugins_enabled", [], type=list) or []
    assert "word_of_the_day" in [str(x) for x in enabled]


def test_toggle_user_plugin_activates_immediately(qapp, tmp_path: Path, qtbot, monkeypatch):
    root = tmp_path / "nb"
    root.mkdir()
    plugdir = root / ".qnotebook" / "plugins"
    plugdir.mkdir(parents=True)
    (plugdir / "myplug.py").write_text(
        "class Plugin:\n"
        "    name = 'My Plug'\n"
        "    description = 'test'\n"
        "    def setup(self, w):\n"
        "        w._myplug_setup_called = True\n",
        encoding="utf-8",
    )
    w = MainWindow()
    w.open_notebook(str(root))
    qtbot.addWidget(w)
    # Enabling a code-executing user plugin must confirm first; auto-accept here.
    monkeypatch.setattr(w, "_confirm_enable_user_plugin", lambda info: True)
    assert not hasattr(w, "_myplug_setup_called")
    w._toggle_plugin("user:myplug", True)
    assert w._myplug_setup_called is True
    # Persisted trust is the content+notebook-bound token, not the bare stem key.
    info = next(i for i in w._plugin_infos if i.key == "user:myplug")
    token = plugins_mod.enabled_token(info)
    assert token is not None and token.startswith("userplugin:")
    s = QSettings("qnotebook", "qnotebook")
    enabled = [str(x) for x in (s.value("plugins_enabled", [], type=list) or [])]
    assert token in enabled
    assert "user:myplug" not in enabled


def test_declining_confirm_does_not_enable_user_plugin(qapp, tmp_path: Path, qtbot, monkeypatch):
    root = tmp_path / "nb"
    root.mkdir()
    plugdir = root / ".qnotebook" / "plugins"
    plugdir.mkdir(parents=True)
    (plugdir / "myplug.py").write_text(
        "class Plugin:\n"
        "    name = 'My Plug'\n"
        "    description = 'test'\n"
        "    def setup(self, w):\n"
        "        w._myplug_setup_called = True\n",
        encoding="utf-8",
    )
    w = MainWindow()
    w.open_notebook(str(root))
    qtbot.addWidget(w)
    monkeypatch.setattr(w, "_confirm_enable_user_plugin", lambda info: False)
    w._toggle_plugin("user:myplug", True)
    assert not hasattr(w, "_myplug_setup_called")
    s = QSettings("qnotebook", "qnotebook")
    enabled = [str(x) for x in (s.value("plugins_enabled", [], type=list) or [])]
    assert enabled == []


def _write_user_plugin(root: Path, stem: str, body: str) -> Path:
    plugdir = root / ".qnotebook" / "plugins"
    plugdir.mkdir(parents=True, exist_ok=True)
    p = plugdir / f"{stem}.py"
    p.write_text(body, encoding="utf-8")
    return p


_BENIGN = (
    "class Plugin:\n"
    "    name = 'Helper'\n"
    "    description = 'benign'\n"
    "    def setup(self, w):\n"
    "        w.helper_ran = True\n"
)


def _malicious(marker: Path) -> str:
    # Writes the marker at *import* time — the exec_module RCE surface.
    return (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('pwned')\n"
        "class Plugin:\n"
        "    name = 'Helper'\n"
        "    description = 'evil'\n"
        "    def setup(self, w):\n"
        "        w.helper_ran = True\n"
    )


def test_cross_notebook_stem_collision_is_not_executed(tmp_path: Path):
    """Trust for notebook A's helper.py must NOT auto-run notebook B's helper.py."""
    marker = tmp_path / "pwned"
    a = tmp_path / "a"
    a.mkdir()
    _write_user_plugin(a, "helper", _BENIGN)
    info_a = next(i for i in plugins_mod.discover(a) if i.key == "user:helper")
    trusted = {plugins_mod.enabled_token(info_a)}  # what the user enabled in A

    b = tmp_path / "b"
    b.mkdir()
    _write_user_plugin(b, "helper", _malicious(marker))
    infos_b = plugins_mod.discover(b)

    window = type("Window", (), {})()
    activated = plugins_mod.setup_enabled(window, infos_b, trusted)
    assert activated == []
    assert not marker.exists()


def test_identical_content_in_other_notebook_still_gated(tmp_path: Path):
    """Even byte-identical content in a different notebook requires fresh consent."""
    a = tmp_path / "a"
    a.mkdir()
    _write_user_plugin(a, "helper", _BENIGN)
    token_a = plugins_mod.enabled_token(
        next(i for i in plugins_mod.discover(a) if i.key == "user:helper")
    )
    b = tmp_path / "b"
    b.mkdir()
    _write_user_plugin(b, "helper", _BENIGN)
    infos_b = plugins_mod.discover(b)
    token_b = plugins_mod.enabled_token(
        next(i for i in infos_b if i.key == "user:helper")
    )
    assert token_a != token_b
    window = type("Window", (), {})()
    assert plugins_mod.setup_enabled(window, infos_b, {token_a}) == []
    assert not hasattr(window, "helper_ran")


def test_editing_trusted_plugin_regates(tmp_path: Path):
    """Modifying an enabled plugin's bytes invalidates its trust token (fail-closed)."""
    root = tmp_path / "nb"
    root.mkdir()
    p = _write_user_plugin(root, "helper", _BENIGN)
    token_old = plugins_mod.enabled_token(
        next(i for i in plugins_mod.discover(root) if i.key == "user:helper")
    )
    marker = tmp_path / "pwned"
    p.write_text(_malicious(marker), encoding="utf-8")
    infos = plugins_mod.discover(root)
    assert plugins_mod.enabled_token(
        next(i for i in infos if i.key == "user:helper")
    ) != token_old
    window = type("Window", (), {})()
    assert plugins_mod.setup_enabled(window, infos, {token_old}) == []
    assert not marker.exists()


def test_content_swap_after_discover_is_revalidated_at_exec(tmp_path: Path):
    """TOCTOU: bytes swapped after discover() must not run under the stale token."""
    root = tmp_path / "nb"
    root.mkdir()
    p = _write_user_plugin(root, "helper", _BENIGN)
    infos = plugins_mod.discover(root)  # content_hash captured for benign bytes
    info = next(i for i in infos if i.key == "user:helper")
    token = plugins_mod.enabled_token(info)  # token bound to the benign hash
    marker = tmp_path / "pwned"
    # Attacker overwrites the file between discovery and execution.
    p.write_text(_malicious(marker), encoding="utf-8")
    window = type("Window", (), {})()
    # setup_enabled sees the stale (benign) content_hash so the token still
    # matches, but _load_user_plugin re-reads and refuses the changed bytes.
    activated = plugins_mod.setup_enabled(window, infos, {token})
    assert activated == []
    assert not marker.exists()


def test_trusted_plugin_reopens_and_runs(tmp_path: Path):
    """The positive path: an unchanged, trusted plugin runs on re-open."""
    root = tmp_path / "nb"
    root.mkdir()
    _write_user_plugin(root, "helper", _BENIGN)
    infos = plugins_mod.discover(root)
    token = plugins_mod.enabled_token(
        next(i for i in infos if i.key == "user:helper")
    )
    # Re-discover to simulate a fresh open with the same on-disk file.
    infos2 = plugins_mod.discover(root)
    window = type("Window", (), {})()
    activated = plugins_mod.setup_enabled(window, infos2, {token})
    assert activated == ["user:helper"]
    assert window.helper_ran is True


def test_stale_stem_setting_does_not_run_or_check_on_open(qapp, tmp_path: Path, qtbot):
    """A pre-fix `user:<stem>` setting must never auto-run a plugin or show checked."""
    root = tmp_path / "nb"
    root.mkdir()
    marker = tmp_path / "pwned"
    _write_user_plugin(root, "helper", _malicious(marker))
    s = QSettings("qnotebook", "qnotebook")
    s.setValue("plugins_enabled", ["user:helper"])  # legacy stem-keyed entry
    s.sync()
    w = MainWindow()
    w.open_notebook(str(root))
    qtbot.addWidget(w)
    assert not marker.exists()
    checked = [a.text() for a in w.m_plugins.actions() if a.isChecked()]
    assert "Helper" not in checked


def test_declining_reverts_only_the_clicked_action_with_duplicate_names(
    qapp, tmp_path: Path, qtbot, monkeypatch
):
    """Two plugins sharing a display name: declining one must not uncheck the other."""
    root = tmp_path / "nb"
    root.mkdir()
    # Two distinct files, identical Plugin.name="Dup".
    for stem in ("a", "b"):
        _write_user_plugin(
            root,
            stem,
            "class Plugin:\n"
            "    name = 'Dup'\n"
            "    description = 'd'\n"
            "    def setup(self, w):\n"
            "        pass\n",
        )
    w = MainWindow()
    w.open_notebook(str(root))
    qtbot.addWidget(w)
    dup_actions = [a for a in w.m_plugins.actions() if a.text() == "Dup"]
    assert len(dup_actions) == 2
    # Accept the first, decline the second.
    monkeypatch.setattr(w, "_confirm_enable_user_plugin", lambda info: True)
    dup_actions[0].trigger()  # toggles checked -> True, accepted
    assert dup_actions[0].isChecked()
    monkeypatch.setattr(w, "_confirm_enable_user_plugin", lambda info: False)
    dup_actions[1].trigger()  # toggles checked -> True then reverted to False
    # The declined action is unchecked; the accepted one is untouched.
    assert not dup_actions[1].isChecked()
    assert dup_actions[0].isChecked()


def test_word_count_plugin_runs(qapp, tmp_notebook: Path, qtbot):
    s = QSettings("qnotebook", "qnotebook")
    s.setValue("plugins_enabled", ["word_of_the_day"])
    s.sync()
    w = MainWindow()
    w.open_notebook(str(tmp_notebook))
    qtbot.addWidget(w)
    # Find the word-count label among the discovered plugin instances.
    labels = []
    for info in w._plugin_infos:
        if info.key == "word_of_the_day":
            label = getattr(info.plugin, "label", None)
            if label is not None:
                labels.append(label)
    assert labels, "word_of_the_day plugin should have a label"
    assert "words:" in labels[0].text()
