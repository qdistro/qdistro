"""URL bar / WebView.navigate smart-URL handling.

Runs through the window fixture so the webview has a real parent and
QtWebEngine cleanup is well-defined.
"""

from unittest.mock import patch


def test_full_url_passes_through(window):
    wv = window._active_webview
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate("https://example.com/foo")
        assert set_url.call_args[0][0].toString() == "https://example.com/foo"


def test_bare_domain_gets_https(window):
    wv = window._active_webview
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate("example.com")
        assert set_url.call_args[0][0].toString().startswith("https://example.com")


def test_search_query_goes_to_engine(window, fresh_config):
    from qdbrowser.config import Config
    Config().set("general", "search_engine",
                 "https://search.invalid/?q={query}")
    wv = window._active_webview
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate("hello world")
        assert set_url.call_args[0][0].toString().startswith(
            "https://search.invalid/?q=hello")


def test_data_url_with_space_is_not_a_search(window):
    """A data: URL has no "://" and often contains spaces; it must load as
    a URL, not be sent (content and all) to the search engine."""
    wv = window._active_webview
    url = "data:text/html;charset=utf-8,<body><h1 id='hi'>Hello</h1></body>"
    with patch.object(wv.view, "setUrl") as set_url:
        wv.navigate(url)
        assert set_url.call_args[0][0].scheme() == "data"
