"""The removable-media mount/unmount actions ride the EXISTING broker
rules engine — no new broker code path. These tests pin that contract:

- ``qdistro.media.mount:<device>`` / ``unmount`` are ordinary action
  strings the RulesEngine matches with its normal selectors.
- An admin can author a glob allow rule (``qdistro.media.mount:*``).
- argv-pinned rules (``argv_exact``) bind a durable approval to ONE
  exact udisksctl command, so a ``forever_argv`` allow of mounting
  /dev/sdb1 does not silently cover mounting a different device.
- A non-matching device / op falls through to None (default-prompt,
  i.e. operationally default-deny until admin acts).

Mirrors the harness layout of test_broker_check_permission.py: drive
the RulesEngine directly against a tmp rules dir.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BROKER = Path(__file__).resolve().parents[2]
# Use the broker from the main checkout's sibling tree if the worktree
# doesn't carry one; here the worktree IS the qdistro tree.
_BROKER_DIR = _BROKER / "broker"
if str(_BROKER_DIR) not in sys.path:
    sys.path.insert(0, str(_BROKER_DIR))

pytest.importorskip("yaml")

from qdistro_admin_rules import RulesEngine  # noqa: E402

UID = 2000
UDISKSCTL = "/usr/bin/udisksctl"


def _mount_argv(device: str) -> list[str]:
    return [UDISKSCTL, "mount", "-b", device]


class TestMediaActionMatching:
    def test_glob_allow_rule_matches_any_device(self, tmp_path):
        d = tmp_path / "rules"
        d.mkdir()
        (d / "media.yaml").write_text(
            "- name: allow-media-mount\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.media.mount:*'\n"
            f"    uid: {UID}\n")
        eng = RulesEngine(str(d))
        r = eng.match(uid=UID, action="qdistro.media.mount:/dev/sdb1",
                      exe="/usr/bin/qdshell")
        assert r is not None and r.decision == "allow"
        # Unmount is a DISTINCT action — the mount rule must NOT match it.
        assert eng.match(uid=UID, action="qdistro.media.unmount:/dev/sdb1",
                         exe="/usr/bin/qdshell") is None

    def test_no_rule_falls_through_to_none(self, tmp_path):
        d = tmp_path / "rules"
        d.mkdir()
        eng = RulesEngine(str(d))
        assert eng.match(uid=UID, action="qdistro.media.mount:/dev/sdb1",
                         exe="/usr/bin/qdshell") is None

    def test_deny_rule_matches(self, tmp_path):
        d = tmp_path / "rules"
        d.mkdir()
        (d / "media.yaml").write_text(
            "- name: deny-media\n"
            "  decision: deny\n"
            "  match:\n"
            "    action: 'qdistro.media.mount:*'\n")
        eng = RulesEngine(str(d))
        r = eng.match(uid=UID, action="qdistro.media.mount:/dev/sdb1",
                      exe="/usr/bin/qdshell")
        assert r is not None and r.decision == "deny"


class TestMediaArgvPinning:
    def test_argv_exact_binds_to_one_device(self, tmp_path):
        d = tmp_path / "rules"
        d.mkdir()
        # A durable allow pinned to mounting EXACTLY /dev/sdb1.
        (d / "media.yaml").write_text(
            "- name: allow-mount-sdb1\n"
            "  decision: allow\n"
            "  scope: forever_argv\n"
            "  match:\n"
            "    action: 'qdistro.media.mount:/dev/sdb1'\n"
            f"    uid: {UID}\n"
            "    argv_exact:\n"
            f"      - '{UDISKSCTL}'\n"
            "      - 'mount'\n"
            "      - '-b'\n"
            "      - '/dev/sdb1'\n")
        eng = RulesEngine(str(d))
        # Exact argv for /dev/sdb1 → allow.
        r = eng.match(uid=UID, action="qdistro.media.mount:/dev/sdb1",
                      exe="/usr/bin/qdshell", argv=_mount_argv("/dev/sdb1"))
        assert r is not None and r.decision == "allow"
        # A DIFFERENT device with the same action prefix does NOT match
        # this argv-pinned rule (the argv tuple differs AND the exact
        # action string differs) → None → re-prompt.
        assert eng.match(uid=UID, action="qdistro.media.mount:/dev/sdc1",
                         exe="/usr/bin/qdshell",
                         argv=_mount_argv("/dev/sdc1")) is None

    def test_argv_rule_requires_argv_present(self, tmp_path):
        d = tmp_path / "rules"
        d.mkdir()
        (d / "media.yaml").write_text(
            "- name: allow-mount-sdb1\n"
            "  decision: allow\n"
            "  match:\n"
            "    action: 'qdistro.media.mount:/dev/sdb1'\n"
            "    argv_exact:\n"
            f"      - '{UDISKSCTL}'\n"
            "      - 'mount'\n"
            "      - '-b'\n"
            "      - '/dev/sdb1'\n")
        eng = RulesEngine(str(d))
        # Selector-presence semantics: a request with NO argv must not
        # match an argv-pinned rule.
        assert eng.match(uid=UID, action="qdistro.media.mount:/dev/sdb1",
                         exe="/usr/bin/qdshell", argv=None) is None
