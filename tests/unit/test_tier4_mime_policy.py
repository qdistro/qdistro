"""Unit tests for tier4_chrome.strip_mimes — the tier-4 clipboard
MIME allow-list that backs the ClipboardGate rich-MIME-leakage defence.

Source: ``todo/gpt-review/tier4-waypipe-display-tests.md`` §"Exercise real
clipboard/view-stream/input paths" -> "Add negative tests for rich MIME
leakage and fail-open transfer."

The s110 bats driver exercises the LIVE ClipboardGate verdict path in a
VM; these pure tests pin the policy the gate relies on, with explicit
NEGATIVE cases for the rich/dangerous MIME types that must NOT survive a
tier-4 -> other-tier transfer (image decoders, HTML handlers, the
gnome-copied-files desktop-drag vector). A regression that widens the
allow-list — i.e. a fail-open leak — turns these red.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "tier4-vm" / "tier4_chrome.py")
_spec = importlib.util.spec_from_file_location("tier4_chrome", _MOD)
tc = importlib.util.module_from_spec(_spec)
sys.modules["tier4_chrome"] = tc
_spec.loader.exec_module(tc)


# --- allow-list: text-only survives ----------------------------------------

class TestAllowed:
    def test_text_plain_survives(self):
        assert tc.strip_mimes(["text/plain"]) == ["text/plain"]

    def test_uri_list_survives(self):
        assert tc.strip_mimes(["text/uri-list"]) == ["text/uri-list"]

    def test_charset_param_matches_base_type(self):
        # text/plain;charset=utf-8 matches on the base type and the
        # original spelling is preserved.
        assert tc.strip_mimes(["text/plain;charset=utf-8"]) == [
            "text/plain;charset=utf-8"]

    def test_order_preserved_and_deduped(self):
        out = tc.strip_mimes(
            ["text/uri-list", "text/plain", "text/plain"])
        assert out == ["text/uri-list", "text/plain"]

    def test_case_insensitive_dedup(self):
        # RFC 2045: MIME types are case-insensitive. TEXT/PLAIN and
        # text/plain must not both survive.
        out = tc.strip_mimes(["TEXT/PLAIN", "text/plain"])
        assert out == ["TEXT/PLAIN"]


# --- NEGATIVE: rich/dangerous MIME leakage must be stripped -----------------

class TestRichMimeLeakageStripped:
    @pytest.mark.parametrize("mime", [
        "image/png",
        "image/jpeg",
        "image/svg+xml",
        "text/html",
        "application/octet-stream",
        "application/x-qt-image",
        "x-special/gnome-copied-files",   # desktop-drag-into-silo vector
        "application/vnd.portal.filetransfer",
    ])
    def test_dangerous_mime_does_not_survive(self, mime):
        assert tc.strip_mimes([mime]) == []

    def test_mixed_offer_keeps_only_text(self):
        # A realistic rich clipboard offer: the gate keeps text, drops
        # the image + html + file-transfer payloads.
        offer = [
            "text/html",
            "image/png",
            "text/plain",
            "x-special/gnome-copied-files",
            "text/uri-list",
        ]
        assert tc.strip_mimes(offer) == ["text/plain", "text/uri-list"]

    def test_html_disguised_as_text_prefix_not_allowed(self):
        # "text/html" starts with "text/" but is NOT in the allow-list;
        # the gate matches on the full base type, not a prefix, so this
        # must be stripped (no fail-open via prefix matching).
        assert tc.strip_mimes(["text/html"]) == []

    def test_empty_and_blank_entries_dropped(self):
        assert tc.strip_mimes(["", "  ", "text/plain"]) == ["text/plain"]

    def test_all_rich_offer_yields_empty(self):
        # If a tier-4 source offers ONLY rich types, the gate yields an
        # empty list — nothing crosses the silo boundary (fail closed).
        assert tc.strip_mimes(["image/png", "text/html"]) == []
