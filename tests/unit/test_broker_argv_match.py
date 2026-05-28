"""task(061) — qsu argv match-kinds end-to-end through the broker.

Covers the wire-level handoff from `qdistro_root_exec`'s
`details["argv[NN]"]` keys through `_argv_from_details` and into
the rules engine's argv selectors (argv_exact / argv_basename /
argv_prefix).

We unit-test `_argv_from_details` directly (no D-Bus) and assert
the broker's `_enqueue` resolves an argv-only rule via the rules
engine — exercising the same code path that fires when qsu
calls RequestPermissionAs.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Match the layout test_broker_check_permission.py uses.
_BROKER = Path(__file__).resolve().parents[1] / "broker"
if str(_BROKER) not in sys.path:
    sys.path.insert(0, str(_BROKER))

from qdistro_admin_broker import _argv_from_details  # noqa: E402


class TestArgvFromDetails:
    def test_basic_zero_padded_indices(self):
        d = {
            "target_user": "root",
            "argv": "id -u",
            "argv[00]": "id",
            "argv[01]": "-u",
        }
        assert _argv_from_details(d) == ["id", "-u"]

    def test_returns_none_when_no_argv_keys(self):
        # Clipboard / handoff calls don't carry argv at all.
        d = {"action": "qdistro.clipboard.transfer", "source_silo": "user1"}
        assert _argv_from_details(d) is None

    def test_returns_none_when_only_shlex_argv_present(self):
        # The shlex-joined `argv` key is human-readable; it must not
        # be mistaken for the lossless `argv[NN]` reconstruction.
        d = {"target_user": "root", "argv": "id -u"}
        assert _argv_from_details(d) is None

    def test_sparse_indices_preserved_as_passed(self):
        # A caller skipping argv[02] preserves the gap as a real list
        # ordering — argv_exact authored against a contiguous list
        # won't match this, which is the desired fail-closed behavior.
        d = {
            "argv[00]": "a",
            "argv[01]": "b",
            "argv[03]": "d",
        }
        assert _argv_from_details(d) == ["a", "b", "d"]

    def test_three_digit_indices_supported(self):
        d = {f"argv[{i:03d}]": f"x{i}" for i in range(0, 5)}
        assert _argv_from_details(d) == ["x0", "x1", "x2", "x3", "x4"]

    def test_indices_above_cap_dropped(self):
        d = {
            "argv[00]": "a",
            "argv[01]": "b",
            "argv[1025]": "way-out-of-range",
        }
        assert _argv_from_details(d) == ["a", "b"]

    def test_unrelated_keys_ignored(self):
        d = {
            "target_user": "root",
            "argv[00]": "id",
            "argv-with-dash": "noise",
            "argv[]": "noise",
            "argvNN": "noise",
            "argv[1": "noise",
        }
        assert _argv_from_details(d) == ["id"]

    def test_missing_argv00_fails_closed_to_none(self):
        # A caller that supplies argv[01]/argv[02] but omits argv[00]
        # has not captured the program element argv-aware scopes pin
        # on. Collapsing to ["/usr/bin/apt-get", "update"] would let an
        # attacker present a program-blind tuple to an argv-pinned
        # scope, so this must read as "no argv captured" (None) and the
        # argv-aware scopes/cache then fail closed.
        d = {
            "target_user": "root",
            "argv[01]": "/usr/bin/apt-get",
            "argv[02]": "update",
        }
        assert _argv_from_details(d) is None

    def test_missing_argv00_with_high_indices_fails_closed(self):
        d = {"argv[05]": "x", "argv[06]": "y"}
        assert _argv_from_details(d) is None

    def test_argv00_present_with_interior_gap_still_returns_list(self):
        # Once argv[00] is captured, an interior gap collapses to
        # "what was actually passed" — unchanged from prior behavior.
        d = {"argv[00]": "a", "argv[02]": "c"}
        assert _argv_from_details(d) == ["a", "c"]

    def test_only_argv00_present(self):
        d = {"argv[00]": "/usr/bin/systemctl"}
        assert _argv_from_details(d) == ["/usr/bin/systemctl"]
