"""Tests for the broker's rules-reload helper (task 059).

`reload_rules_from_disk(source=...)` is the central reload entry
point shared by SIGHUP, the inotify watch, and the D-Bus
ReloadRules method. Pinning behaviour here so the various
trigger paths can't drift apart over time:

  - reloads the rules from disk
  - logs source-tagged
  - emits RulesReloaded with the new count

Inotify wiring + SIGHUP wiring themselves are tested behaviorally
via the broker's logs in the bats suite (the kernel side isn't
worth mocking out at unit level).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("dbus")

import dbus  # noqa: E402

from test_broker_check_permission import _StubBroker  # noqa: E402


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


def _write(rules_dir: Path, name: str, body: str) -> None:
    (rules_dir / name).write_text(body)


def test_reload_picks_up_new_file(broker: _StubBroker, rules_dir: Path):
    assert len(broker.rules.rules()) == 0
    _write(rules_dir, "a.yaml",
           "- name: a\n"
           "  decision: allow\n"
           "  match: { action: x.y:z }\n")
    n = broker.reload_rules_from_disk(source="test")
    assert n == 1
    assert broker.rules.rules()[0].name == "a"


def test_reload_drops_deleted_file(broker: _StubBroker, rules_dir: Path):
    _write(rules_dir, "a.yaml",
           "- name: a\n"
           "  decision: allow\n"
           "  match: { action: x.y:z }\n")
    broker.reload_rules_from_disk(source="setup")
    assert len(broker.rules.rules()) == 1
    (rules_dir / "a.yaml").unlink()
    n = broker.reload_rules_from_disk(source="test")
    assert n == 0
    assert broker.rules.rules() == []


def test_reload_emits_rules_reloaded_signal(broker: _StubBroker,
                                            rules_dir: Path):
    pre = list(broker.rules_reloaded_signals)
    _write(rules_dir, "a.yaml",
           "- name: a\n"
           "  decision: deny\n"
           "  match: { action: x.y:z }\n")
    n = broker.reload_rules_from_disk(source="test")
    assert n == 1
    assert broker.rules_reloaded_signals[len(pre):] == [1]


def test_reload_signal_count_reflects_loaded_rules(
        broker: _StubBroker, rules_dir: Path):
    _write(rules_dir, "a.yaml",
           "- name: a\n  decision: allow\n  match: { action: x:y:z }\n"
           "- name: b\n  decision: deny\n  match: { action: x:y:z }\n")
    n = broker.reload_rules_from_disk(source="test")
    assert n == 2
    assert broker.rules_reloaded_signals[-1] == 2


def test_reload_multiple_times_idempotent(broker: _StubBroker,
                                          rules_dir: Path):
    _write(rules_dir, "a.yaml",
           "- name: a\n  decision: allow\n  match: { action: x:y:z }\n")
    for _ in range(3):
        n = broker.reload_rules_from_disk(source=f"test{_}")
        assert n == 1
    # Three reloads, three RulesReloaded emits.
    assert broker.rules_reloaded_signals[-3:] == [1, 1, 1]


def test_reload_with_broken_yaml_keeps_valid_rules(
        broker: _StubBroker, rules_dir: Path):
    _write(rules_dir, "a.yaml",
           "- name: a\n  decision: allow\n  match: { action: x:y:z }\n")
    _write(rules_dir, "b.yaml", "this is not yaml at all: [oops")
    n = broker.reload_rules_from_disk(source="test")
    # The valid file still loads; the broken one shows up in
    # load_errors but doesn't take down the engine.
    assert n == 1
    errs = broker.rules.load_errors()
    assert any("b.yaml" in e for e in errs)


def test_directory_accessor_returns_configured_dir(
        broker: _StubBroker, rules_dir: Path):
    # task(059) exposes RulesEngine.directory() so the broker's
    # inotify wiring knows what path to watch.
    assert Path(broker.rules.directory()) == rules_dir
