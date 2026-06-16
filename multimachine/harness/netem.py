"""Named netem profiles for the two-VM display harness.

09 / codex r6: use *named* profiles so docs say "passed under profile X", never
"passed Wi-Fi". This module is the single definition of those profiles plus the
``tc`` command builders to apply/clear them on an inter-VM link. The builders
are pure (return argv lists) so they unit-test without root or a real NIC; the
qci two-VM glue runs them in-guest/on the host bridge.

Exact parameters live here (not in prose) so an evidence bundle records the
profile name and the harness can reproduce it.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NetemProfile:
    name: str
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    reorder_pct: float = 0.0
    rate_kbit: int | None = None  # bandwidth cap
    description: str = ""
    # ``disconnect`` is modelled as a hard drop the harness toggles, not a
    # steady netem state; flagged so the runner knows to flap the link.
    hard_drop: bool = False

    def tc_add(self, dev: str) -> list[str]:
        """argv to add this profile as a root netem qdisc on ``dev``."""
        cmd = ["tc", "qdisc", "add", "dev", dev, "root", "netem"]
        if self.delay_ms:
            cmd += ["delay", f"{self.delay_ms}ms"]
            if self.jitter_ms:
                cmd += [f"{self.jitter_ms}ms"]
        if self.loss_pct:
            cmd += ["loss", f"{self.loss_pct}%"]
        if self.reorder_pct:
            # reorder needs a delay to be meaningful; tc requires it.
            if not self.delay_ms:
                cmd += ["delay", "1ms"]
            cmd += ["reorder", f"{100 - self.reorder_pct}%"]
        if self.rate_kbit:
            cmd += ["rate", f"{self.rate_kbit}kbit"]
        return cmd

    def tc_del(self, dev: str) -> list[str]:
        return ["tc", "qdisc", "del", "dev", dev, "root"]


# The five named profiles (09 / codex r6). Values are deliberate and recorded.
PROFILES: dict[str, NetemProfile] = {
    "lan-clean": NetemProfile("lan-clean", delay_ms=1.0,
                              description="low latency, no loss"),
    "lan-loaded": NetemProfile("lan-loaded", delay_ms=5.0, jitter_ms=2.0,
                               description="low latency + jitter + queue pressure"),
    "wifi-good": NetemProfile("wifi-good", delay_ms=15.0, jitter_ms=5.0,
                              loss_pct=0.1,
                              description="moderate latency/jitter, rare loss"),
    "wifi-bad": NetemProfile("wifi-bad", delay_ms=40.0, jitter_ms=20.0,
                             loss_pct=2.0, rate_kbit=20000,
                             description="high jitter, burst loss, bw cap"),
    "disconnect": NetemProfile("disconnect", hard_drop=True,
                               description="hard drop, flap, delayed reconnect"),
}


def profile(name: str) -> NetemProfile:
    if name not in PROFILES:
        raise KeyError(f"unknown netem profile {name!r}; "
                       f"known: {sorted(PROFILES)}")
    return PROFILES[name]
