"""Tests for qdistro_print_browse — cups-browsed allowlist renderer.

Pure-python module: extract_print_discover_rules picks rules of the
form ``qdistro.print.discover.<host>``, render_cups_browsed_conf
emits a default-deny allowlist body, render_from_broker_rules wires
the two together.
"""
from __future__ import annotations

from qdistro_print_browse import (
    extract_print_discover_rules, render_cups_browsed_conf,
    render_from_broker_rules,
)


# -- extract_print_discover_rules -------------------------------------------

class TestExtract:
    def test_picks_only_print_discover(self):
        rules = [
            {"action": "qdistro.print.discover.printer.example.com",
             "decision": "allow", "name": "office-laser"},
            {"action": "qdistro.print.access",
             "decision": "allow", "name": "default-print"},
            {"action": "qdistro.clipboard.transfer",
             "decision": "deny", "name": "clip-block"},
        ]
        out = extract_print_discover_rules(rules)
        assert len(out) == 1
        assert out[0]["host"] == "printer.example.com"
        assert out[0]["decision"] == "allow"
        assert out[0]["name"] == "office-laser"

    def test_skips_non_allow_deny(self):
        rules = [
            {"action": "qdistro.print.discover.foo", "decision": "ask"},
            {"action": "qdistro.print.discover.bar", "decision": ""},
        ]
        assert extract_print_discover_rules(rules) == []

    def test_strips_prefix(self):
        rules = [{"action": "qdistro.print.discover.10.0.0.5",
                  "decision": "allow"}]
        assert extract_print_discover_rules(rules)[0]["host"] == "10.0.0.5"

    def test_handles_glob_host(self):
        rules = [{"action": "qdistro.print.discover.192.168.1.*",
                  "decision": "allow"}]
        assert extract_print_discover_rules(rules)[0]["host"] == "192.168.1.*"

    def test_empty_input(self):
        assert extract_print_discover_rules([]) == []

    def test_decision_normalised_lower(self):
        rules = [{"action": "qdistro.print.discover.x", "decision": "ALLOW"}]
        assert extract_print_discover_rules(rules)[0]["decision"] == "allow"


# -- render_cups_browsed_conf -----------------------------------------------

class TestRenderConf:
    def test_default_deny_when_empty(self):
        body = render_cups_browsed_conf()
        assert "BrowseAllow none" in body
        assert "BrowseProtocols cups dnssd" in body

    def test_emits_each_allow(self):
        body = render_cups_browsed_conf(allow_hosts=["a.example.com",
                                                     "10.0.0.5"])
        assert "BrowseAllow a.example.com" in body
        assert "BrowseAllow 10.0.0.5" in body
        assert "BrowseAllow none" not in body

    def test_emits_each_deny(self):
        body = render_cups_browsed_conf(allow_hosts=["a.example.com"],
                                        deny_hosts=["evil.example.com"])
        assert "BrowseDeny evil.example.com" in body

    def test_strips_whitespace_in_hosts(self):
        body = render_cups_browsed_conf(allow_hosts=["  pad.example  "])
        assert "BrowseAllow pad.example" in body

    def test_no_header_option(self):
        body = render_cups_browsed_conf(include_header=False)
        assert not body.startswith("#")

    def test_header_documents_origin(self):
        body = render_cups_browsed_conf()
        assert "broker" in body
        assert "qdistro.print.discover" in body


# -- render_from_broker_rules -----------------------------------------------

class TestRenderFromBrokerRules:
    def test_round_trip_default_deny(self):
        body = render_from_broker_rules([])
        assert "BrowseAllow none" in body

    def test_allow_one_host(self):
        rules = [{"action": "qdistro.print.discover.printer.local",
                  "decision": "allow"}]
        body = render_from_broker_rules(rules)
        assert "BrowseAllow printer.local" in body
        assert "BrowseAllow none" not in body

    def test_mixed_allow_deny(self):
        rules = [
            {"action": "qdistro.print.discover.10.0.0.5", "decision": "allow"},
            {"action": "qdistro.print.discover.evil", "decision": "deny"},
            {"action": "qdistro.unrelated.action", "decision": "allow"},
        ]
        body = render_from_broker_rules(rules)
        assert "BrowseAllow 10.0.0.5" in body
        assert "BrowseDeny evil" in body
        # Non-print rules don't bleed in.
        assert "qdistro.unrelated" not in body
