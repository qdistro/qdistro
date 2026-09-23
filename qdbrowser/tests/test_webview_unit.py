"""WebView accessors + state. All tests use the window fixture so the
view has a real parent."""

from unittest.mock import patch


def test_default_url_is_homepage(window):
    wv = window._active_webview
    assert wv.url() in ("about:blank", "")


def test_default_zoom(window):
    assert window._active_webview.zoom() == 1.0


def test_set_zoom_clamps_low(window):
    wv = window._active_webview
    wv.set_zoom(0.01)
    assert wv.zoom() == 0.25


def test_set_zoom_clamps_high(window):
    wv = window._active_webview
    wv.set_zoom(99.0)
    assert wv.zoom() == 5.0


def test_set_zoom_normal(window):
    wv = window._active_webview
    wv.set_zoom(1.5)
    assert wv.zoom() == 1.5


def test_set_muted(window):
    wv = window._active_webview
    wv.set_muted(True)
    assert wv.muted is True
    wv.set_muted(False)
    assert wv.muted is False


def test_set_pinned(window):
    wv = window._active_webview
    wv.set_pinned(True)
    assert wv.pinned is True
    wv.set_pinned(False)
    assert wv.pinned is False


def test_group_default_none(window):
    assert window._active_webview.group is None


def test_assign_group(window):
    wv = window._active_webview
    wv.group = "work"
    assert wv.group == "work"


def test_page_load_seq_starts_at_zero(window):
    assert window._active_webview.page_load_seq() == 0


def test_profile_name_default(window):
    assert window._active_webview.profile_name == "default"


def test_default_webview_not_off_the_record(window):
    # The persistent "default" profile keeps data on disk.
    assert window._active_webview.is_off_the_record is False


def test_private_webview_is_off_the_record(window):
    # A "private" tab is backed by an off-the-record QWebEngineProfile.
    wv = window.new_tab(url="about:blank", profile_name="private")
    assert wv.profile_name == "private"
    assert wv.is_off_the_record is True


def test_off_the_record_reads_live_profile_flag(window):
    # The flag must follow the real Qt profile, not just the name string,
    # so a private profile wired in under another name still reports OTR.
    wv = window._active_webview
    assert wv.is_off_the_record == bool(wv._profile.isOffTheRecord())


def test_can_go_back_false_initially(window):
    assert window._active_webview.can_go_back() is False


def test_can_go_forward_false_initially(window):
    assert window._active_webview.can_go_forward() is False


def test_empty_navigate_is_noop(window):
    wv = window._active_webview
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate("")
        wv.navigate("   ")
        set_url.assert_not_called()


def test_about_blank_passes_through(window):
    wv = window._active_webview
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate("about:blank")
        assert set_url.call_args[0][0].toString() == "about:blank"


def test_navigate_with_path_only_treated_as_search(window):
    wv = window._active_webview
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate("query with spaces")
        url = set_url.call_args[0][0].toString()
        assert "spaces" in url
        assert url.startswith("https://")


def test_reload_calls_view(window):
    wv = window._active_webview
    with patch.object(wv.view, "reload") as r:
        wv.reload()
        r.assert_called_once()


def test_stop_calls_view(window):
    wv = window._active_webview
    with patch.object(wv.view, "stop") as s:
        wv.stop()
        s.assert_called_once()


def test_go_back_calls_view(window):
    wv = window._active_webview
    with patch.object(wv.view, "back") as b:
        wv.go_back()
        b.assert_called_once()


def test_go_forward_calls_view(window):
    wv = window._active_webview
    with patch.object(wv.view, "forward") as f:
        wv.go_forward()
        f.assert_called_once()


def test_add_remove_interceptor(window):
    wv = window._active_webview

    class FakeInterc:
        def intercept(self, info):
            pass

    h = FakeInterc()
    wv.add_interceptor(h)
    assert h in wv._interceptor._handlers
    wv.remove_interceptor(h)
    assert h not in wv._interceptor._handlers


def test_remove_unknown_interceptor_no_error(window):
    wv = window._active_webview

    class FakeInterc:
        def intercept(self, info):
            pass

    wv.remove_interceptor(FakeInterc())  # should not raise
