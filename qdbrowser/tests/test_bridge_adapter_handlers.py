"""Track-02 unit tests for the bridge_adapter D-Bus handler surface.

These exercise the pure-Python dispatcher with mocked proxies. The
goal is to nail the protocol contract — method names, argument
counts, return signatures, polkit gating — without standing up a real
session bus.
"""

from __future__ import annotations

import pytest
from qdbrowser.plugins import bridge_adapter as ba

# --------------------------------------------------------------------- #
# Tiny fakes for the four proxies.
# --------------------------------------------------------------------- #


class _FakeTabs:
    def __init__(self):
        self._tabs = [(1, "Home", "https://example.com"),
                      (2, "Docs", "https://docs.example.com")]
        self.opened: list = []
        self.closed: list = []

    def list(self):
        return list(self._tabs)

    def open(self, url):
        new_id = max((t[0] for t in self._tabs), default=0) + 1
        self._tabs.append((new_id, "New", url))
        self.opened.append(url)
        return new_id

    def close(self, tab_id):
        before = len(self._tabs)
        self._tabs = [t for t in self._tabs if t[0] != tab_id]
        self.closed.append(tab_id)
        return len(self._tabs) < before


class _FakePages:
    def __init__(self):
        self.last_call = None

    def extract(self, tab_id, mode):
        self.last_call = (tab_id, mode)
        return (f"title-{tab_id}", f"https://t/{tab_id}",
                f"content-{mode}")


class _FakeDownloads:
    def list(self):
        return [(0, "report.pdf", 1), (1, "movie.mkv", 2)]


class _FakeMedia:
    def status(self):
        return ("My Track", "An Artist", "playing")


class _FakeHistory:
    def search(self, query, limit=50):
        data = [
            ("https://example.com", "Example", "2026-01-01T00:00:00+00:00"),
            ("https://docs.example.com", "Docs", "2026-01-02T00:00:00+00:00"),
            ("https://news.example.com", "News", "2026-01-03T00:00:00+00:00"),
        ]
        q = query.lower().strip()
        out = []
        for url, title, ts in data:
            if q and q not in url.lower() and q not in title.lower():
                continue
            out.append((url, title, ts))
            if limit and len(out) >= limit:
                break
        return out


class _FakeBookmarks:
    def search(self, query, limit=50):
        data = [
            ("https://saved.example.com", "Saved Page"),
            ("https://blog.example.com", "My Blog"),
        ]
        q = query.lower().strip()
        out = []
        for url, title in data:
            if q and q not in url.lower() and q not in title.lower():
                continue
            out.append((url, title))
            if limit and len(out) >= limit:
                break
        return out


def _make_handlers(polkit_allow=True, history=None, bookmarks=None):
    return ba.BridgeAdapterHandlers(
        _FakeTabs(), _FakePages(), _FakeDownloads(), _FakeMedia(),
        polkit=lambda action, pid: polkit_allow,
        history=history,
        bookmarks=bookmarks,
    )


# --------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------- #


def test_introspection_xml_lists_all_methods():
    xml = ba.QDBROWSER_INTROSPECTION_XML
    for method in ("TabsList", "TabsOpen", "TabsClose",
                   "PageExtract", "DownloadsList", "MediaStatus",
                   "HistorySearch", "BookmarksSearch"):
        assert f'name="{method}"' in xml
    # Signal names must match the emit_* helpers.
    for sig in ("TabAdded", "TabRemoved",
                "DownloadStarted", "MediaStateChanged"):
        assert f'name="{sig}"' in xml


def test_method_to_action_table_is_complete():
    # Every method we introspect must map to a polkit action.
    for method in ("TabsList", "TabsOpen", "TabsClose",
                   "PageExtract", "DownloadsList", "MediaStatus",
                   "HistorySearch", "BookmarksSearch"):
        assert method in ba.METHOD_TO_ACTION


def test_tabs_list_returns_signature_and_payload():
    h = _make_handlers()
    body, sig = h.dispatch("TabsList", ())
    assert sig == "a(uss)"
    (tabs,) = body
    assert tabs == [(1, "Home", "https://example.com"),
                    (2, "Docs", "https://docs.example.com")]


def test_tabs_open_returns_new_id():
    h = _make_handlers()
    body, sig = h.dispatch("TabsOpen", ("https://new.example",))
    assert sig == "u"
    (new_id,) = body
    assert new_id == 3
    assert h.tabs.opened == ["https://new.example"]


def test_tabs_close_returns_bool():
    h = _make_handlers()
    body, sig = h.dispatch("TabsClose", (1,))
    assert sig == "b"
    assert body == (True,)
    # Closing a missing tab → False.
    body, _ = h.dispatch("TabsClose", (999,))
    assert body == (False,)


def test_page_extract_modes_round_trip():
    h = _make_handlers()
    body, sig = h.dispatch("PageExtract", (1, "text"))
    assert sig == "sss"
    title, url, content = body
    assert title == "title-1"
    assert url == "https://t/1"
    assert content == "content-text"
    # html / selection also round-trip.
    for mode in ("html", "selection"):
        body, _ = h.dispatch("PageExtract", (1, mode))
        assert body[2] == f"content-{mode}"


def test_downloads_list_signature():
    h = _make_handlers()
    body, sig = h.dispatch("DownloadsList", ())
    assert sig == "a(usu)"
    (dls,) = body
    assert dls == [(0, "report.pdf", 1), (1, "movie.mkv", 2)]


def test_media_status_returns_three_strings():
    h = _make_handlers()
    body, sig = h.dispatch("MediaStatus", ())
    assert sig == "sss"
    assert body == ("My Track", "An Artist", "playing")


def test_unknown_method_raises():
    h = _make_handlers()
    with pytest.raises(LookupError):
        h.dispatch("NoSuchMethod", ())


@pytest.mark.cheat_aware(
    protects="a mutating bridge method is refused (PermissionError) when "
             "polkit denies the caller — the permission boundary is enforced "
             "before dispatch, not after",
    severity="critical",
    cheats=[
        "change pytest.raises(PermissionError) to assert it returns instead",
        "switch the method to a read-only one that bypasses the polkit gate",
        "set polkit_allow=True to dodge the deny path",
    ],
    consequence="an unauthorized caller drives the browser (opens tabs / "
                "navigates) through the bridge with no policy check",
)
def test_polkit_denies_blocks_dispatch():
    h = _make_handlers(polkit_allow=False)
    # Mutating method → blocked.
    with pytest.raises(PermissionError):
        h.dispatch("TabsOpen", ("https://x",))


def test_polkit_skipped_for_internal_caller():
    """caller_pid=None means 'internal call' — read-only actions still
    pass, mutating ones still consult the polkit hook (which here is
    a stub that returns True)."""
    h = _make_handlers(polkit_allow=True)
    body, _ = h.dispatch("TabsList", (), caller_pid=None)
    (tabs,) = body
    assert len(tabs) == 2


def test_polkit_check_short_circuits_read_only():
    """polkit_check skips pkcheck for read-only inventory actions even
    if a caller PID is supplied — the policy file says allow:yes."""
    assert ba.polkit_check("org.qdistro.qdbrowser.tabs.list", 1) is True
    assert ba.polkit_check("org.qdistro.qdbrowser.media.status", 1) is True
    assert ba.polkit_check("org.qdistro.qdbrowser.downloads.list", 1) is True


def test_polkit_check_internal_call_bypass():
    """caller_pid=None always returns True (internal call)."""
    assert ba.polkit_check("org.qdistro.qdbrowser.tabs.open", None) is True


def test_polkit_check_uses_pkcheck_for_mutating(monkeypatch):
    """For mutating actions with a real caller pid, pkcheck is shelled
    out to. We monkeypatch subprocess.run to a fake."""
    calls: list = []

    class _FakeResult:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = b""
            self.stderr = b""

    def fake_run(args, capture_output=True, timeout=None):
        calls.append(args)
        return _FakeResult(0)

    monkeypatch.setattr(ba.subprocess, "run", fake_run)
    assert ba.polkit_check("org.qdistro.qdbrowser.tabs.open", 4242) is True
    assert any("org.qdistro.qdbrowser.tabs.open" in a for a in calls[0])
    assert "4242" in calls[0]


def test_polkit_check_pkcheck_denies(monkeypatch):
    class _FakeResult:
        def __init__(self):
            self.returncode = 1
            self.stdout = b""
            self.stderr = b""

    monkeypatch.setattr(ba.subprocess, "run",
                        lambda *a, **kw: _FakeResult())
    assert ba.polkit_check(
        "org.qdistro.qdbrowser.tabs.open", 4242) is False


def test_polkit_check_pkcheck_oserror_denies(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no pkcheck on PATH")
    monkeypatch.setattr(ba.subprocess, "run", boom)
    assert ba.polkit_check(
        "org.qdistro.qdbrowser.tabs.open", 4242) is False


# --------------------------------------------------------------------- #
# Finding #12: history / bookmarks search are NOT unauthenticated.
# --------------------------------------------------------------------- #


def test_history_bookmarks_not_in_open_actions():
    """HistorySearch/BookmarksSearch must require authorization — they
    expose privacy-sensitive browsing metadata, so their action ids
    must NOT short-circuit through _OPEN_ACTIONS."""
    hist = ba.METHOD_TO_ACTION["HistorySearch"]
    book = ba.METHOD_TO_ACTION["BookmarksSearch"]
    assert hist not in ba._OPEN_ACTIONS
    assert book not in ba._OPEN_ACTIONS
    # Only the live-session inventory ops stay open.
    assert ba._OPEN_ACTIONS == {
        "org.qdistro.qdbrowser.tabs.list",
        "org.qdistro.qdbrowser.media.status",
        "org.qdistro.qdbrowser.downloads.list",
    }


def test_polkit_check_does_not_short_circuit_history_bookmarks(monkeypatch):
    """With a real caller pid, history/bookmarks actions must consult
    pkcheck (not return True for free). We make pkcheck deny and assert
    the call is refused."""
    class _Denied:
        returncode = 1
        stdout = b""
        stderr = b""

    monkeypatch.setattr(ba.subprocess, "run", lambda *a, **kw: _Denied())
    assert ba.polkit_check(
        "org.qdistro.qdbrowser.history.search", 4242) is False
    assert ba.polkit_check(
        "org.qdistro.qdbrowser.bookmarks.search", 4242) is False


def test_history_search_denied_when_polkit_denies():
    """A non-internal caller whose polkit check fails cannot read
    history through dispatch()."""
    h = _make_handlers(polkit_allow=False, history=_FakeHistory())
    with pytest.raises(PermissionError):
        h.dispatch("HistorySearch", ("anything", 50), caller_pid=4242)


def test_bookmarks_search_denied_when_polkit_denies():
    h = _make_handlers(polkit_allow=False, bookmarks=_FakeBookmarks())
    with pytest.raises(PermissionError):
        h.dispatch("BookmarksSearch", ("anything", 50), caller_pid=4242)


def _load_policy_action_ids():
    import os
    import xml.etree.ElementTree as ET
    here = os.path.dirname(os.path.abspath(__file__))
    policy = os.path.join(here, "..", "polkit",
                          "org.qdistro.qdbrowser.policy")
    tree = ET.parse(policy)
    return {a.get("id") for a in tree.getroot().findall("action")}


def test_every_mapped_action_has_a_policy_entry():
    """Each method's polkit action id must exist in the policy XML —
    otherwise a non-open action would be undefined and polkit would
    deny it by implicit-default with no operator-visible declaration
    (and an open action would silently bypass review). Closes the
    history/bookmarks gap from finding #12."""
    policy_ids = _load_policy_action_ids()
    for method, action in ba.METHOD_TO_ACTION.items():
        if action in ba._OPEN_ACTIONS:
            # Open actions are allow:yes; still expect them declared.
            assert action in policy_ids, (
                f"open action {action} for {method} missing from policy")
        else:
            assert action in policy_ids, (
                f"gated action {action} for {method} missing from policy")


def test_history_bookmarks_policy_requires_authorization():
    """The new history/bookmarks actions must require at least active-
    user authorization (auth_*) — never allow:yes."""
    import os
    import xml.etree.ElementTree as ET
    here = os.path.dirname(os.path.abspath(__file__))
    policy = os.path.join(here, "..", "polkit",
                          "org.qdistro.qdbrowser.policy")
    root = ET.parse(policy).getroot()
    for action_id in ("org.qdistro.qdbrowser.history.search",
                      "org.qdistro.qdbrowser.bookmarks.search"):
        node = next(a for a in root.findall("action")
                    if a.get("id") == action_id)
        active = node.find("defaults/allow_active").text
        assert active.startswith("auth_"), (
            f"{action_id} allow_active={active!r} is not an auth gate")


def test_search_limit_is_clamped():
    """Oversized / bogus limits are clamped so a single authorized
    query can't drain unbounded browsing metadata (finding #12)."""
    assert ba._clamp_search_limit(10_000) == ba._MAX_SEARCH_LIMIT
    assert ba._clamp_search_limit(0) == ba._MAX_SEARCH_LIMIT
    assert ba._clamp_search_limit(-5) == ba._MAX_SEARCH_LIMIT
    assert ba._clamp_search_limit("nope") == ba._MAX_SEARCH_LIMIT
    assert ba._clamp_search_limit(25) == 25


# --------------------------------------------------------------------- #
# Proxy unit tests — verify the duck-typed adapters.
# --------------------------------------------------------------------- #


class _FakeWebView:
    def __init__(self, tid, title, url, is_off_the_record=False):
        self.stable_id = tid
        self._title = title
        self._url = url
        self.is_off_the_record = is_off_the_record

    def title(self):
        return self._title

    def url(self):
        return self._url


class _FakeSplit:
    def __init__(self, views):
        self._views = views

    def find_webviews(self):
        return list(self._views)


class _FakeTabsWidget:
    def __init__(self, splits):
        self._splits = splits

    def count(self):
        return len(self._splits)

    def widget(self, i):
        return self._splits[i]


class _FakeWindow:
    def __init__(self, splits):
        self._tabs = _FakeTabsWidget(splits)
        self.opened: list = []
        self.closed: list = []
        self._next_id = 100

    def new_tab(self, url, **kw):
        wv = _FakeWebView(self._next_id, "x", url)
        self._next_id += 1
        self._tabs._splits.append(_FakeSplit([wv]))
        self.opened.append(url)
        return wv

    def _on_tab_close_requested(self, idx):
        self.closed.append(idx)
        self._tabs._splits.pop(idx)


def test_tabs_proxy_list_open_close():
    wv1 = _FakeWebView(1, "T1", "https://1/")
    wv2 = _FakeWebView(2, "T2", "https://2/")
    win = _FakeWindow([_FakeSplit([wv1]), _FakeSplit([wv2])])
    proxy = ba.TabsProxy(win)
    assert proxy.list() == [(1, "T1", "https://1/"),
                             (2, "T2", "https://2/")]
    new_id = proxy.open("https://3/")
    assert new_id == 100
    assert win.opened == ["https://3/"]
    assert proxy.close(2) is True
    assert win.closed == [1]
    assert proxy.close(9999) is False


def test_pages_proxy_extract_invokes_run_js():
    wv = _FakeWebView(5, "Title", "https://5/")
    win = _FakeWindow([_FakeSplit([wv])])
    seen: list = []

    def fake_run_js(wv, script):
        seen.append(script)
        return "BODY-TEXT"

    proxy = ba.PagesProxy(win, run_js=fake_run_js)
    title, url, content = proxy.extract(5, "text")
    assert title == "Title"
    assert url == "https://5/"
    assert content == "BODY-TEXT"
    assert "innerText" in seen[0]


def test_pages_proxy_unknown_mode_rejected():
    win = _FakeWindow([])
    proxy = ba.PagesProxy(win, run_js=None)
    with pytest.raises(ValueError):
        proxy.extract(1, "screenshot")


def test_pages_proxy_missing_tab_raises():
    win = _FakeWindow([])
    proxy = ba.PagesProxy(win, run_js=lambda *a: "")
    with pytest.raises(LookupError):
        proxy.extract(999, "text")


# --------------------------------------------------------------------- #
# 02/S9 — private (off-the-record) tabs must not reach the bridge.
# --------------------------------------------------------------------- #


def test_tabs_proxy_list_hides_off_the_record():
    pub = _FakeWebView(1, "Public", "https://pub/")
    priv = _FakeWebView(2, "Secret", "https://secret/", is_off_the_record=True)
    win = _FakeWindow([_FakeSplit([pub]), _FakeSplit([priv])])
    proxy = ba.TabsProxy(win)
    listed = proxy.list()
    assert listed == [(1, "Public", "https://pub/")]
    assert all(tid != 2 for (tid, _t, _u) in listed)
    assert all("secret" not in url for (_i, _t, url) in listed)


def test_pages_proxy_extract_denied_for_off_the_record():
    priv = _FakeWebView(7, "Secret", "https://secret/", is_off_the_record=True)
    win = _FakeWindow([_FakeSplit([priv])])
    called: list = []
    proxy = ba.PagesProxy(win, run_js=lambda wv, s: called.append(s) or "X")
    with pytest.raises(PermissionError):
        proxy.extract(7, "text")
    assert called == [], "OTR extract must not even run the page JS"


def test_tabs_proxy_close_denied_for_off_the_record():
    """02/S9: TabsClose must refuse a private tab by id — hiding it from the
    list is not an authorization boundary (ids are monotonic/guessable)."""
    priv = _FakeWebView(2, "Secret", "https://secret/", is_off_the_record=True)
    win = _FakeWindow([_FakeSplit([priv])])
    proxy = ba.TabsProxy(win)
    with pytest.raises(PermissionError):
        proxy.close(2)
    assert win.closed == [], "OTR tab must NOT be closed"


def test_downloads_proxy_empty_when_no_plugin():
    proxy = ba.DownloadsProxy(None)
    assert proxy.list() == []


def test_downloads_proxy_hides_private_downloads():
    """02/S9: a private (off-the-record) download must not appear in the bridge
    DownloadsList — its filename/state/timing is the same leak as its origin."""
    class _Req:
        def state(self):
            return 1

    class _W:
        def __init__(self, path, private):
            self._request = _Req()
            self._private = private
            self._p = path

        def path(self):
            return self._p

    class _Panel:
        _items = [(None, _W("/d/public.zip", False)),
                  (None, _W("/d/secret.pdf", True))]
        _history: list = []

    class _Plug:
        _panel = _Panel()

    rows = ba.DownloadsProxy(_Plug()).list()
    names = [name for (_i, name, _s) in rows]
    assert names == ["public.zip"]
    assert "secret.pdf" not in names


def test_downloads_proxy_falls_back_to_request_when_marker_absent():
    """02/S9 fallback: a widget without the _private marker is classified via
    the plugin's _request_is_off_the_record (fail closed → private skipped)."""
    class _Req:
        def __init__(self, otr):
            self.otr = otr

        def state(self):
            return 1

    class _W:
        def __init__(self, path, otr):
            self._request = _Req(otr)
            self._p = path
            # NOTE: deliberately no _private marker.

        def path(self):
            return self._p

    class _Panel:
        _items = [(None, _W("/d/public.zip", False)),
                  (None, _W("/d/secret.pdf", True))]
        _history: list = []

    class _Plug:
        _panel = _Panel()

        @staticmethod
        def _request_is_off_the_record(req):
            return bool(getattr(req, "otr", True))  # fail closed

    rows = ba.DownloadsProxy(_Plug()).list()
    names = [name for (_i, name, _s) in rows]
    assert names == ["public.zip"]


def test_media_proxy_update_and_status():
    media = ba.MediaProxy()
    assert media.status() == ("", "", "stopped")
    media.update("Song", "Band", "playing")
    assert media.status() == ("Song", "Band", "playing")


# --------------------------------------------------------------------- #
# History + Bookmarks proxy and dispatch tests (step 3).
# --------------------------------------------------------------------- #


def test_history_search_dispatch_returns_correct_signature():
    h = _make_handlers(history=_FakeHistory())
    body, sig = h.dispatch("HistorySearch", ("example", 50))
    assert sig == "a(sss)"
    (results,) = body
    assert len(results) == 3
    # Each result is (url, title, timestamp).
    assert results[0] == ("https://example.com", "Example",
                          "2026-01-01T00:00:00+00:00")


def test_history_search_filters_by_query():
    h = _make_handlers(history=_FakeHistory())
    body, _ = h.dispatch("HistorySearch", ("docs", 50))
    (results,) = body
    assert len(results) == 1
    assert results[0][0] == "https://docs.example.com"


def test_history_search_respects_limit():
    h = _make_handlers(history=_FakeHistory())
    body, _ = h.dispatch("HistorySearch", ("", 2))
    (results,) = body
    assert len(results) == 2


def test_history_search_empty_when_no_proxy():
    h = _make_handlers(history=None)
    body, sig = h.dispatch("HistorySearch", ("test", 10))
    assert sig == "a(sss)"
    (results,) = body
    assert results == []


def test_bookmarks_search_dispatch_returns_correct_signature():
    h = _make_handlers(bookmarks=_FakeBookmarks())
    body, sig = h.dispatch("BookmarksSearch", ("", 50))
    assert sig == "a(ss)"
    (results,) = body
    assert len(results) == 2
    assert results[0] == ("https://saved.example.com", "Saved Page")


def test_bookmarks_search_filters_by_query():
    h = _make_handlers(bookmarks=_FakeBookmarks())
    body, _ = h.dispatch("BookmarksSearch", ("blog", 50))
    (results,) = body
    assert len(results) == 1
    assert results[0][0] == "https://blog.example.com"


def test_bookmarks_search_respects_limit():
    h = _make_handlers(bookmarks=_FakeBookmarks())
    body, _ = h.dispatch("BookmarksSearch", ("", 1))
    (results,) = body
    assert len(results) == 1


def test_bookmarks_search_empty_when_no_proxy():
    h = _make_handlers(bookmarks=None)
    body, sig = h.dispatch("BookmarksSearch", ("test", 10))
    assert sig == "a(ss)"
    (results,) = body
    assert results == []


# --------------------------------------------------------------------- #
# HistoryProxy / BookmarksProxy duck-typed adapter tests.
# --------------------------------------------------------------------- #


class _FakeHistoryStore:
    """Mimics the _Store class from history.py."""
    def __init__(self, records):
        self._records = records

    def all(self):
        return list(reversed(self._records))


class _FakeHistoryPlugin:
    def __init__(self, records):
        self._store = _FakeHistoryStore(records)


def test_history_proxy_search_matches_url_and_title():
    records = [
        {"url": "https://a.com", "title": "Alpha", "ts": 1700000000},
        {"url": "https://b.com", "title": "Beta", "ts": 1700001000},
        {"url": "https://c.com", "title": "Gamma", "ts": 1700002000},
    ]
    proxy = ba.HistoryProxy(_FakeHistoryPlugin(records))
    # Search by URL substring.
    results = proxy.search("b.com")
    assert len(results) == 1
    assert results[0][0] == "https://b.com"
    # Search by title.
    results = proxy.search("alpha")
    assert len(results) == 1
    assert results[0][1] == "Alpha"
    # Empty query returns all.
    results = proxy.search("")
    assert len(results) == 3


def test_history_proxy_formats_timestamp_as_iso():
    records = [{"url": "https://x.com", "title": "X", "ts": 0}]
    proxy = ba.HistoryProxy(_FakeHistoryPlugin(records))
    results = proxy.search("")
    assert len(results) == 1
    # ts=0 is 1970-01-01T00:00:00+00:00
    assert "1970" in results[0][2]


def test_history_proxy_returns_empty_for_none_plugin():
    proxy = ba.HistoryProxy(None)
    assert proxy.search("test") == []


def test_history_proxy_respects_limit():
    records = [
        {"url": f"https://{i}.com", "title": f"T{i}", "ts": i}
        for i in range(10)
    ]
    proxy = ba.HistoryProxy(_FakeHistoryPlugin(records))
    results = proxy.search("", limit=3)
    assert len(results) == 3


class _FakeBookmarksPanel:
    def __init__(self, bookmarks):
        self._bookmarks = bookmarks

    def all(self):
        return list(self._bookmarks)


class _FakeBookmarksPlugin:
    def __init__(self, bookmarks):
        self._panel = _FakeBookmarksPanel(bookmarks)


def test_bookmarks_proxy_search_matches_url_and_title():
    bmarks = [
        {"url": "https://x.com", "title": "Xray"},
        {"url": "https://y.com", "title": "Yankee"},
    ]
    proxy = ba.BookmarksProxy(_FakeBookmarksPlugin(bmarks))
    results = proxy.search("xray")
    assert len(results) == 1
    assert results[0] == ("https://x.com", "Xray")
    # Empty query returns all.
    results = proxy.search("")
    assert len(results) == 2


def test_bookmarks_proxy_returns_empty_for_none_plugin():
    proxy = ba.BookmarksProxy(None)
    assert proxy.search("test") == []


def test_bookmarks_proxy_respects_limit():
    bmarks = [{"url": f"https://{i}.com", "title": f"B{i}"}
              for i in range(10)]
    proxy = ba.BookmarksProxy(_FakeBookmarksPlugin(bmarks))
    results = proxy.search("", limit=4)
    assert len(results) == 4


# --------------------------------------------------------------------- #
# Polkit: history + bookmarks now REQUIRE authorization (finding #12).
# They used to short-circuit through _OPEN_ACTIONS; that allowed any
# session-bus caller to bulk-read browsing metadata without a prompt.
# --------------------------------------------------------------------- #


def test_polkit_check_history_and_bookmarks_consult_pkcheck(monkeypatch):
    """With a real caller pid, history/bookmarks must go through pkcheck
    rather than returning True for free. Here pkcheck grants, proving
    the enforcement path is actually exercised."""
    seen: list = []

    class _Granted:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(args, **kw):
        seen.append(args)
        return _Granted()

    monkeypatch.setattr(ba.subprocess, "run", fake_run)
    assert ba.polkit_check(
        "org.qdistro.qdbrowser.history.search", 1) is True
    assert ba.polkit_check(
        "org.qdistro.qdbrowser.bookmarks.search", 1) is True
    # Both calls shelled out to pkcheck (no _OPEN_ACTIONS bypass).
    assert any("org.qdistro.qdbrowser.history.search" in a for a in seen)
    assert any("org.qdistro.qdbrowser.bookmarks.search" in a for a in seen)


# --------------------------------------------------------------------- #
# Receive loop unit tests (mock-level, no real bus).
# --------------------------------------------------------------------- #


def test_recv_loop_dispatches_method_call(monkeypatch):
    """Verify _recv_loop routes a method_call message to the handlers
    and sends the reply, using mock objects for jeepney."""

    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"

    # Build handlers with fakes.
    plugin._handlers = _make_handlers(history=_FakeHistory(),
                                      bookmarks=_FakeBookmarks())
    plugin._stop = __import__("threading").Event()

    # Build a fake message and a fake connection.
    from unittest.mock import MagicMock

    fake_msg = MagicMock()
    fake_msg.header.message_type = MagicMock()
    fake_msg.header.message_type.__eq__ = lambda self, other: (
        str(other) == "MessageType.method_call")
    fake_msg.header.serial = 42
    fake_msg.header.fields = {
        2: ba.QDBROWSER_IFACE,   # HeaderFields.interface
        1: ba.QDBROWSER_PATH,    # HeaderFields.path
        3: "TabsList",           # HeaderFields.member
        7: ":1.100",             # HeaderFields.sender
    }
    fake_msg.body = ()

    call_count = 0
    sent_replies = []

    class FakeConn:
        sock = MagicMock()

        def receive(self, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fake_msg
            # After first message, signal stop.
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent_replies.append(msg)

    class FakePidConn:
        def send_and_get_reply(self, msg, timeout=None):
            # For GetConnectionUnixProcessID — return a fake PID.
            result = MagicMock()
            result.body = (1234,)
            return result

    plugin._conn = FakeConn()
    plugin._pid_conn = FakePidConn()

    # Monkeypatch jeepney imports used by _recv_loop.
    # We need the real MessageType/HeaderFields enums to match.
    from jeepney import HeaderFields, MessageType
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.fields = {
        HeaderFields.interface: ba.QDBROWSER_IFACE,
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "TabsList",
        HeaderFields.sender: ":1.100",
    }

    # Monkeypatch select to always say "ready".
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))

    # Run the recv loop (it will process one message then stop).
    plugin._recv_loop()

    assert len(sent_replies) == 1
    # The reply should be a method_return (we can check it was constructed).
    reply = sent_replies[0]
    assert reply.header.message_type == MessageType.method_return


def test_recv_loop_returns_error_for_unknown_method(monkeypatch):
    """Verify _recv_loop sends an error reply for unknown methods."""
    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"
    plugin._handlers = _make_handlers()
    plugin._stop = __import__("threading").Event()

    from unittest.mock import MagicMock

    from jeepney import HeaderFields, MessageType

    fake_msg = MagicMock()
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.serial = 99
    fake_msg.header.fields = {
        HeaderFields.interface: ba.QDBROWSER_IFACE,
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "NoSuchMethod",
        HeaderFields.sender: ":1.200",
    }
    fake_msg.body = ()

    call_count = 0
    sent_replies = []

    class FakeConn:
        sock = MagicMock()

        def receive(self, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fake_msg
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent_replies.append(msg)

    class FakePidConn:
        def send_and_get_reply(self, msg, timeout=None):
            result = MagicMock()
            result.body = (5678,)
            return result

    plugin._conn = FakeConn()
    plugin._pid_conn = FakePidConn()
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))

    plugin._recv_loop()

    assert len(sent_replies) == 1
    reply = sent_replies[0]
    assert reply.header.message_type == MessageType.error


def test_recv_loop_handles_introspect(monkeypatch):
    """Verify _recv_loop responds to Introspect with the XML."""
    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"
    plugin._handlers = _make_handlers()
    plugin._stop = __import__("threading").Event()

    from unittest.mock import MagicMock

    from jeepney import HeaderFields, MessageType

    fake_msg = MagicMock()
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.serial = 10
    fake_msg.header.fields = {
        HeaderFields.interface: "org.freedesktop.DBus.Introspectable",
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "Introspect",
        HeaderFields.sender: ":1.300",
    }
    fake_msg.body = ()

    call_count = 0
    sent_replies = []

    class FakeConn:
        sock = MagicMock()

        def receive(self, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fake_msg
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent_replies.append(msg)

    plugin._conn = FakeConn()
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))

    plugin._recv_loop()

    assert len(sent_replies) == 1
    reply = sent_replies[0]
    assert reply.header.message_type == MessageType.method_return
    assert ba.QDBROWSER_INTROSPECTION_XML in reply.body


@pytest.mark.cheat_aware(
    protects="when the caller's PID cannot be resolved, a mutating method "
             "from an external sender is DENIED (AccessDenied), not allowed "
             "to fall through — caller identity must be known to authorize",
    severity="critical",
    cheats=[
        "stop asserting the reply is MessageType.error / AccessDenied",
        "provide a fake _pid_conn so the deny branch is never exercised",
        "assert on a read-only method that skips the polkit gate",
    ],
    consequence="an unidentifiable caller bypasses authorization and drives "
                "the browser bridge as if it were trusted",
)
def test_recv_loop_denies_when_pid_resolution_fails(monkeypatch):
    """When _pid_conn is None, mutating methods from external callers
    must be denied (PermissionError → AccessDenied D-Bus error)."""
    plugin = ba.BridgeAdapterPlugin()
    plugin._active = True
    plugin._bus_name = "org.qdistro.QdBrowser.pid1"
    plugin._handlers = _make_handlers()
    plugin._stop = __import__("threading").Event()

    from unittest.mock import MagicMock

    from jeepney import HeaderFields, MessageType

    fake_msg = MagicMock()
    fake_msg.header.message_type = MessageType.method_call
    fake_msg.header.serial = 77
    fake_msg.header.fields = {
        HeaderFields.interface: ba.QDBROWSER_IFACE,
        HeaderFields.path: ba.QDBROWSER_PATH,
        HeaderFields.member: "TabsOpen",
        HeaderFields.sender: ":1.500",
    }
    fake_msg.body = ("https://example.com",)

    call_count = 0
    sent_replies = []

    class FakeConn:
        sock = MagicMock()

        def receive(self, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return fake_msg
            plugin._stop.set()
            raise TimeoutError

        def send(self, msg):
            sent_replies.append(msg)

    plugin._conn = FakeConn()
    # Deliberately leave _pid_conn as None to simulate failure.
    plugin._pid_conn = None
    monkeypatch.setattr(ba.select, "select",
                        lambda r, w, x, t: (r, w, x))

    plugin._recv_loop()

    assert len(sent_replies) == 1
    reply = sent_replies[0]
    assert reply.header.message_type == MessageType.error
    # The error should be AccessDenied.
    from jeepney import HeaderFields as HF
    assert "AccessDenied" in reply.header.fields.get(
        HF.error_name, "")


def test_resolve_sender_pid_raises_on_failure():
    """_resolve_sender_pid raises PermissionError when the PID cannot
    be resolved, preventing auth bypass."""
    plugin = ba.BridgeAdapterPlugin()
    plugin._pid_conn = None  # No PID connection.

    with pytest.raises(PermissionError):
        plugin._resolve_sender_pid(":1.123")


def test_resolve_sender_pid_returns_none_for_no_sender():
    """No sender (internal call) should return None, not raise."""
    plugin = ba.BridgeAdapterPlugin()
    assert plugin._resolve_sender_pid(None) is None
