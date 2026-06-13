"""greetd / greeter no-network hardening (qdgreeter finding 02).

greetd spawns the unprivileged `_greeter` to render the boot login, so greetd's
systemd hardening drop-in propagates to the greeter. qdistro authenticates
against LOCAL PAM only, so neither greetd nor the greeter needs an inet socket.
This pins that `deploy/greetd-hardening.conf` (installed as
/etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf) denies network
access, so the pre-auth greeter cannot reach the network. Reads the checked-in
file only — the live effective property is VM-verified separately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARDENING = ROOT / "deploy/greetd-hardening.conf"


@pytest.mark.cheat_aware(
    protects="greetd + the _greeter it spawns cannot open an inet socket "
    "(RestrictAddressFamilies reset-then-AF_UNIX/AF_NETLINK + IPAddressDeny=any); "
    "the pre-auth boot greeter has no network path",
    severity="medium",
    cheats=[
        "drop IPAddressDeny=any or the RestrictAddressFamilies lines",
        "omit the empty RestrictAddressFamilies= reset so a vendor allow-list "
        "widens the policy",
        "add IPAddressAllow=any which is checked before the deny list",
    ],
    consequence="the pre-auth greeter (and greetd) can reach the network, "
    "violating the TCB no-network discipline the threat model requires",
)
def test_greetd_drop_in_denies_network():
    text = HARDENING.read_text(encoding="utf-8")
    # IP egress denied.
    assert re.search(r"^\s*IPAddressDeny\s*=\s*any\s*$", text, re.MULTILINE), (
        "greetd-hardening.conf must set IPAddressDeny=any"
    )
    # A stray allow-list would be checked BEFORE the deny list and weaken it.
    assert not re.search(r"^\s*IPAddressAllow\s*=", text, re.MULTILINE), (
        "greetd-hardening.conf must not set IPAddressAllow (it overrides the deny)"
    )
    # Address families: an empty reset must precede the exact allow-list so the
    # drop-in OWNS the policy (drop-ins MERGE otherwise and could widen it).
    fams = re.findall(r"^\s*RestrictAddressFamilies\s*=(.*)$", text, re.MULTILINE)
    assert len(fams) >= 2, (
        "expected a RestrictAddressFamilies= reset followed by the allow-list"
    )
    assert fams[0].strip() == "", (
        "the first RestrictAddressFamilies= must be an empty reset"
    )
    assert "AF_UNIX" in fams[-1] and "AF_NETLINK" in fams[-1], (
        "the effective RestrictAddressFamilies must allow AF_UNIX + AF_NETLINK"
    )
    # And must NOT re-admit inet.
    assert "AF_INET" not in text, "greeter must not be allowed AF_INET/AF_INET6"
