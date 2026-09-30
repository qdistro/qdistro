"""permissions-gui/17: the shipped guest helper captures the broker request id.

The 2026-09-30 full run lost scenario 17 as a harness ERROR because the runner
re-typed S1's GetPending parser with the wrong JSON shape and recorded
BROKER_REQUEST_ID=none. The parser now ships as 17-relay.sh; these tests run
that script (not a copy of it) against busctl's --json=short rendering of the
broker's aa{sv} reply.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tests" / "integration" / "permissions-gui" / "17-relay.sh"
SCENARIO = ROOT / "tests" / "integration" / "permissions-gui" / "17-realapp-sendto-deny.md"
ACTION = "app.send-to:3000:org.qdistro.Qnotebook.uid3000"


def _row(rid: int, uid: int, action: str) -> dict:
    # busctl --json=short wraps every a{sv} value as {"type": ..., "data": ...}
    return {
        "id": {"type": "i", "data": rid},
        "uid": {"type": "i", "data": uid},
        "pid": {"type": "i", "data": 4242},
        "exe": {"type": "s", "data": "/usr/bin/dbus-send"},
        "action": {"type": "s", "data": action},
        "details": {"type": "a{ss}", "data": {"payload": "please_deny_me"}},
        "layered_pending": {"type": "b", "data": False},
    }


def _pending_id(rows: list[dict] | str) -> subprocess.CompletedProcess:
    stdin = rows if isinstance(rows, str) else json.dumps({"type": "aa{sv}", "data": [rows]})
    return subprocess.run(
        ["bash", str(HELPER), "pending-id", ACTION, "2000"],
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_unique_matching_row_yields_its_id():
    proc = _pending_id([_row(7, 2000, ACTION), _row(8, 2000, "test.action"), _row(9, 1000, ACTION)])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "7"


def test_no_match_or_several_matches_is_not_an_id():
    none = _pending_id([_row(8, 2000, "test.action")])
    assert (none.returncode, none.stdout.strip()) == (1, "none")
    both = _pending_id([_row(7, 2000, ACTION), _row(11, 2000, ACTION)])
    assert (both.returncode, both.stdout.strip()) == (1, "ambiguous")
    garbage = _pending_id("Failed to connect to bus")
    assert (garbage.returncode, garbage.stdout.strip()) == (1, "none")


def test_scenario_runs_the_shipped_helper_not_inline_copies():
    text = SCENARIO.read_text()
    assert "17-relay.sh" in text
    assert "bash /tmp/17-relay.sh send" in text
    assert "bash /tmp/17-relay.sh verdict" in text
    # the logic lives only in the helper now
    assert "GetPending 2>/dev/null" not in text
    assert "VERDICT_B64" not in text
