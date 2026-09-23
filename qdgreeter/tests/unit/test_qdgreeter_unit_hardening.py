"""Debug systemd unit hardening (finding 02).

systemd/qdgreeter.service is a DEBUG/diagnostic unit (production runs qdgreeter
via greetd's [default_session], not this), but it ships runnable. A pre-auth,
raw-input, PAM-touching greeter unit must not ship under-sandboxed, or a stray
`systemctl enable` hands it the network and an unconfined filesystem. These
checks read the checked-in unit only — no VM, no systemd.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_FILE = REPO_ROOT / "systemd" / "qdgreeter.service"


def _unit_text() -> str:
    return UNIT_FILE.read_text(encoding="utf-8")


@pytest.mark.cheat_aware(
    protects="the debug qdgreeter.service carries the TCB baseline (no network, "
    "no new privileges, strict FS, no core dumps), so enabling it does not hand "
    "the pre-auth greeter an unconfined unit",
    severity="low-medium",
    cheats=[
        "drop the network restriction directives",
        "remove NoNewPrivileges / ProtectSystem",
        "leave LimitCORE unset so a crash can dump the password buffer",
    ],
    consequence="someone `systemctl enable`s an under-sandboxed greeter that can "
    "reach the network and dump pre-auth password state",
)
def test_debug_unit_is_sandboxed():
    text = _unit_text()
    required = [
        r"^\s*PrivateNetwork\s*=\s*yes\s*$",
        r"^\s*RestrictAddressFamilies\s*=\s*AF_UNIX AF_NETLINK\s*$",
        r"^\s*IPAddressDeny\s*=\s*any\s*$",
        r"^\s*NoNewPrivileges\s*=\s*yes\s*$",
        r"^\s*ProtectSystem\s*=\s*strict\s*$",
        # ProtectSystem=strict must be paired with a /run+/dev re-open so the
        # eglfs greeter can still write its KMS cursor config / reach DRM nodes.
        r"^\s*ReadWritePaths\s*=.*/run(?:\s|$)",
        r"^\s*ProtectHome\s*=\s*read-only\s*$",
        r"^\s*SystemCallArchitectures\s*=\s*native\s*$",
        r"^\s*LimitCORE\s*=\s*0\s*$",
        r"^\s*Environment\s*=\s*PYTHONNOUSERSITE=1\s*$",
    ]
    for pat in required:
        assert re.search(pat, text, re.MULTILINE), (
            f"qdgreeter.service must contain a line matching {pat!r}"
        )
