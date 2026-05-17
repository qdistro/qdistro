"""Tests for qdistro_recall_admin — first concrete consumer of
qdistro_browser_bridge_client.call_via_relay.

Each test swaps the underlying _BaseDBusClient on the client module
so no real bus is needed; the test then verifies that the recall
admin layer drives call_via_relay correctly and joins the live
state into the recall rows the way an admin caller would expect.
"""
from __future__ import annotations

import json

import pytest

import qdistro_browser_bridge_client as _client
import qdistro_recall_admin as RA


class _FakeDBus(_client._BaseDBusClient):
    def __init__(self, *, replies: dict[tuple, str] | None = None):
        self.calls: list[dict] = []
        self._replies = replies or {}

    def list_names(self, bus):  # noqa: ARG002
        return []

    def call(self, bus, service, object_path, interface, method,
             signature, body):
        self.calls.append({
            "bus": bus, "service": service, "method": method,
            "body": body,
        })
        key = (bus, service, method)
        return self._replies.get(key, json.dumps({"ok": True}))


@pytest.fixture(autouse=True)
def _reset_client():
    _client.set_dbus_client(None)
    yield
    _client.set_dbus_client(None)


# ---- list_user_containers ------------------------------------------------

class TestListUserContainers:
    def test_routes_through_relay_with_any_selector(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True,
                "containers": [
                    {"cookie_store_id": "firefox-container-1",
                     "name": "Personal"},
                ],
            }),
        })
        _client.set_dbus_client(fake)
        reply = RA.list_user_containers(2000)
        assert reply["ok"] is True
        assert reply["containers"][0]["name"] == "Personal"
        # One call to the relay with the right shape.
        assert len(fake.calls) == 1
        call = fake.calls[0]
        op, _args, sel = call["body"]
        assert op == "containers.list"
        assert json.loads(sel) == {"any": True}
        assert call["service"] == "org.qdistro.UserRelay.uid2000"

    def test_chromium_unavailable_propagates(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid3000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False,
                "error": "contextualIdentities_unavailable",
                "containers": [],
            }),
        })
        _client.set_dbus_client(fake)
        reply = RA.list_user_containers(3000)
        assert reply["ok"] is False
        assert reply["error"] == "contextualIdentities_unavailable"

    def test_no_bridge_found_propagates(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid4000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False,
                "error": "no_bridge_found",
                "selector": {"any": True},
            }),
        })
        _client.set_dbus_client(fake)
        reply = RA.list_user_containers(4000)
        assert reply["error"] == "no_bridge_found"


# ---- list_user_tabs ------------------------------------------------------

class TestListUserTabs:
    def test_calls_tabs_list_op(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True,
                "tabs": [
                    {"id": 7, "url": "https://x/", "title": "X",
                     "active": True, "window_id": 1},
                ],
            }),
        })
        _client.set_dbus_client(fake)
        reply = RA.list_user_tabs(2000)
        assert reply["ok"] is True
        assert reply["tabs"][0]["url"] == "https://x/"
        op, _, _ = fake.calls[0]["body"]
        assert op == "tabs.list"


# ---- annotate_with_live_tabs ---------------------------------------------

class TestAnnotateWithLiveTabs:
    def _fake_with_tabs(self, tabs):
        return _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "tabs": tabs,
            }),
        })

    def test_match_by_exact_url(self):
        rows = [
            {"id": 1, "url": "https://a.example/article",
             "title": "Article", "text": "..."},
            {"id": 2, "url": "https://b.example/",
             "title": "B", "text": "..."},
        ]
        tabs = [
            {"id": 7, "url": "https://a.example/article",
             "title": "Article", "active": True, "window_id": 1},
        ]
        _client.set_dbus_client(self._fake_with_tabs(tabs))
        reply = RA.annotate_with_live_tabs(rows, 2000)
        assert reply["ok"] is True
        out = reply["rows"]
        assert out[0]["live_tab"]["id"] == 7
        assert out[1]["live_tab"] is None

    def test_input_rows_not_mutated(self):
        rows = [{"id": 1, "url": "https://a/", "text": "..."}]
        _client.set_dbus_client(self._fake_with_tabs(
            [{"id": 7, "url": "https://a/", "title": "A"}]))
        RA.annotate_with_live_tabs(rows, 2000)
        assert "live_tab" not in rows[0]

    def test_duplicate_tab_urls_first_wins(self):
        """The same URL open in two tabs picks the first one. Audit
        callers that need *all* matches can use list_user_tabs() and
        do their own join."""
        rows = [{"id": 1, "url": "https://dup/", "text": "..."}]
        tabs = [
            {"id": 7, "url": "https://dup/", "title": "first"},
            {"id": 8, "url": "https://dup/", "title": "second"},
        ]
        _client.set_dbus_client(self._fake_with_tabs(tabs))
        reply = RA.annotate_with_live_tabs(rows, 2000)
        assert reply["rows"][0]["live_tab"]["id"] == 7

    def test_row_without_url_gets_none(self):
        rows = [{"id": 1, "text": "..."}]  # no url
        _client.set_dbus_client(self._fake_with_tabs([]))
        reply = RA.annotate_with_live_tabs(rows, 2000)
        assert reply["rows"][0]["live_tab"] is None

    def test_row_with_non_string_url_gets_none(self):
        rows = [{"id": 1, "url": None, "text": "..."},
                {"id": 2, "url": 42, "text": "..."}]
        _client.set_dbus_client(self._fake_with_tabs([]))
        reply = RA.annotate_with_live_tabs(rows, 2000)
        assert reply["rows"][0]["live_tab"] is None
        assert reply["rows"][1]["live_tab"] is None

    def test_relay_failure_returns_rows_unchanged_with_error(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": False, "error": "no_bridge_found",
            }),
        })
        _client.set_dbus_client(fake)
        rows = [{"id": 1, "url": "https://x/", "text": "..."}]
        reply = RA.annotate_with_live_tabs(rows, 2000)
        assert reply["ok"] is False
        assert reply["error"] == "no_bridge_found"
        # Rows are passed through so the admin UI can still render
        # historical results when the live lookup is unreachable.
        assert reply["rows"] == rows
        assert "live_tab" not in reply["rows"][0]

    def test_iterable_input_consumed_once(self):
        """Caller may pass a generator; we materialise it before
        making the relay call so a relay failure doesn't drop the
        rows."""
        def gen():
            yield {"id": 1, "url": "https://a/", "text": "..."}
            yield {"id": 2, "url": "https://b/", "text": "..."}
        _client.set_dbus_client(self._fake_with_tabs([
            {"id": 9, "url": "https://b/", "title": "B"},
        ]))
        reply = RA.annotate_with_live_tabs(gen(), 2000)
        assert len(reply["rows"]) == 2
        assert reply["rows"][1]["live_tab"]["id"] == 9

    def test_bad_tabs_payload_returns_bad_reply(self):
        fake = _FakeDBus(replies={
            ("SYSTEM", "org.qdistro.UserRelay.uid2000",
             "ForwardBrowserBridgeOp"): json.dumps({
                "ok": True, "tabs": "not-a-list",
            }),
        })
        _client.set_dbus_client(fake)
        rows = [{"id": 1, "url": "https://x/"}]
        reply = RA.annotate_with_live_tabs(rows, 2000)
        assert reply["ok"] is False
        assert reply["error"] == "bad_reply"
        assert reply["rows"] == rows

    def test_empty_rows_still_returns_ok(self):
        _client.set_dbus_client(self._fake_with_tabs([]))
        reply = RA.annotate_with_live_tabs([], 2000)
        assert reply["ok"] is True
        assert reply["rows"] == []
