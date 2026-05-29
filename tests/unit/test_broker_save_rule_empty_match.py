"""Broker-side refusal of empty-match ``allow`` rules in SaveRule.

Source-of-truth contract (security-hardening-carryforward §"Broker and
rules"): an admin-generated ``decision: allow`` rule must carry at
least one *real* selector. An empty / missing ``match:`` block under a
``decision: allow`` rule matches every (uid, action, exe, ...) tuple
and silently grants everything, so ``SaveRule`` — the authoritative
broker-side surface for programmatic rule creation — must refuse it
*before* persisting (a typed ``RulesEngineRefused`` D-Bus error).

These tests are intentionally narrow about what counts as "no real
selector": only an empty or all-empty ``match`` block is refused. A
``deny`` rule with the same empty match is *accepted* (denying broadly
only ever restricts, never widens), and a properly-selectored ``allow``
is accepted. We deliberately do NOT assert that a broad ``"*"`` glob is
refused: per the rules-engine schema docstring (§4 "Empty-selector
safeguard"), file/admin-authored wildcard selectors are admin-trusted
and pass the non-empty check by design — rejecting them here would be a
false-refusal regression on legitimate blanket admin rules.

Mirrors the test_broker_save_rule.py harness shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

from test_broker_check_permission import (  # noqa: E402
    _StubBroker, ADMIN_UID,
)


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rules"
    d.mkdir()
    return d


@pytest.fixture
def broker(tmp_path: Path, rules_dir: Path) -> _StubBroker:
    return _StubBroker(
        str(tmp_path / "approvals.sqlite"),
        str(tmp_path / "audit.sqlite"),
        str(rules_dir),
    )


def _is_refused(exc_info) -> bool:
    msg = str(exc_info.value)
    return "non-empty match" in msg.lower() or "RulesEngineRefused" in msg


class TestEmptyMatchAllowRefused:
    """An allow rule with no real selector must be refused server-side."""

    def test_allow_empty_match_block_refused(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.SaveRule(
                "allow-empty.yaml",
                "- decision: allow\n"
                "  match: {}\n",
            )
        assert _is_refused(ei)
        # Nothing was persisted.
        assert not (rules_dir / "allow-empty.yaml").exists()

    def test_allow_missing_match_refused(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.SaveRule(
                "allow-nomatch.yaml",
                "- decision: allow\n",
            )
        assert _is_refused(ei)
        assert not (rules_dir / "allow-nomatch.yaml").exists()

    def test_allow_all_empty_selector_values_refused(self, broker, rules_dir):
        """A match block present but with only empty/null selector
        values is still effectively allow-all and must be refused."""
        broker.set_peer(uid=ADMIN_UID)
        with pytest.raises(dbus.DBusException) as ei:
            broker.SaveRule(
                "allow-blank.yaml",
                "- decision: allow\n"
                "  match:\n"
                "    action: ''\n"
                "    exe: ''\n",
            )
        assert _is_refused(ei)
        assert not (rules_dir / "allow-blank.yaml").exists()


class TestProperlySelectoredAllowAccepted:
    """A real selector on an allow rule passes — no false refusal."""

    def test_allow_with_action_selector_accepted(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        path = broker.SaveRule(
            "allow-action.yaml",
            "- decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.view-stream.subscribe:test'\n",
        )
        assert Path(path).exists()

    def test_allow_with_uid_selector_accepted(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        path = broker.SaveRule(
            "allow-uid.yaml",
            "- decision: allow\n"
            "  match:\n"
            "    uid: 1234\n",
        )
        assert Path(path).exists()

    def test_allow_with_exe_selector_accepted(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        path = broker.SaveRule(
            "allow-exe.yaml",
            "- decision: allow\n"
            "  match:\n"
            "    exe: '/usr/bin/foo'\n",
        )
        assert Path(path).exists()


class TestDenyEmptyMatchAccepted:
    """A deny rule only restricts, so an empty match is safe to accept."""

    def test_deny_empty_match_accepted(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        path = broker.SaveRule(
            "deny-all.yaml",
            "- decision: deny\n"
            "  match: {}\n",
        )
        assert Path(path).exists()

    def test_deny_missing_match_accepted(self, broker, rules_dir):
        broker.set_peer(uid=ADMIN_UID)
        path = broker.SaveRule(
            "deny-nomatch.yaml",
            "- decision: deny\n",
        )
        assert Path(path).exists()
