"""Content blocker: hosts parsing + suffix matching."""



def test_suffix_match():
    from qdbrowser.plugins.content_blocker import _suffix_match
    s = {"example.com", "ads.bad.com"}
    assert _suffix_match("foo.example.com", s)
    assert _suffix_match("tracking.foo.example.com", s)
    assert _suffix_match("ads.bad.com", s)
    assert not _suffix_match("notexample.com", s)
    assert not _suffix_match("safe.com", s)


def test_hosts_file_parsing(tmp_path):
    from qdbrowser.plugins.content_blocker import _parse_hosts
    p = tmp_path / "hosts"
    p.write_text(
        "# comment\n"
        "0.0.0.0 ads.example.com\n"
        "127.0.0.1 tracker.com\n"
        "\n"
        "single.line\n"
        "  # indented comment\n"
    )
    hosts = _parse_hosts(str(p))
    assert hosts == {"ads.example.com", "tracker.com", "single.line"}


def test_blocker_activates(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()

    class _Win:
        pass

    plug.activate(_Win())
    assert plug._enabled is True
    # Stats initialize to zero (cosmetic_hidden added in the EasyList rewrite).
    assert plug.stats["blocked"] == 0
    assert plug.stats["allowed"] == 0
