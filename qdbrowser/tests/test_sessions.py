"""Sessions: save / list / load."""



def test_save_and_list(window, monkeypatch, tmp_path):
    import qdbrowser.plugins.sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "SESSIONS_DIR",
                        str(tmp_path / "sessions"))
    monkeypatch.setattr(sessions_mod, "_path",
                        lambda n: str(tmp_path / "sessions" / (n + ".json")))

    sessions_mod.save_session(window, "alpha")
    names = sessions_mod.list_sessions()
    assert "alpha" in names


def test_save_then_load_restores_url_count(window, monkeypatch, tmp_path):
    import qdbrowser.plugins.sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "SESSIONS_DIR",
                        str(tmp_path / "sessions"))
    monkeypatch.setattr(sessions_mod, "_path",
                        lambda n: str(tmp_path / "sessions" / (n + ".json")))

    # Build a known shape: 2 tabs.
    window.new_tab(url="about:blank")
    assert window._tabs.count() == 2
    sessions_mod.save_session(window, "two-tabs")

    # Now blow it away.
    while window._tabs.count() > 0:
        window._tabs.removeTab(0)
    window.new_tab()
    assert window._tabs.count() == 1

    ok = sessions_mod.load_session(window, "two-tabs")
    assert ok
    assert window._tabs.count() == 2
