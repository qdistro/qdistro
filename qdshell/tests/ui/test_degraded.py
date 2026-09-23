"""§1 degraded real-service states — VM-only.

Panels must render a coherent UNAVAILABLE / EMPTY state when the backing
service is absent — no Bluetooth adapter, offline network, no PipeWire, no
battery, no media player, no tray items — rather than a blank panel, stale
data, or a crash. A typical qdwin VM is itself a degraded host (no battery, no
BT adapter, no MPRIS player, no tray apps), so this is the natural environment
to pin graceful degradation.

Each case opens the panel, screenshots the live framebuffer, and judges the
description against a degraded-state golden in expectations/*_degraded.md. The
golden encodes the load-bearing invariant: header/close present, an explicit
empty/disabled message, and explicitly NO fabricated populated content.

VM-only: needs the live qdwin session (the host nested-compositor path
SIGSEGVs on headless Wayland).
"""

from dataclasses import dataclass

import pytest

from . import runner


@dataclass(frozen=True)
class DegradedCase:
    id: str
    open_cmd: list
    close_cmd: list
    expectation: str
    service: str


DEGRADED_CASES = [
    DegradedCase("bluetooth", ["bluetooth", "togglePanel"], ["bluetooth", "togglePanel"],
                 "panel_bluetooth_degraded.md", "no Bluetooth adapter / BT off"),
    DegradedCase("network", ["network", "togglePanel"], ["network", "togglePanel"],
                 "panel_network_degraded.md", "offline network"),
    DegradedCase("audio", ["audio", "togglePanel"], ["audio", "togglePanel"],
                 "panel_audio_degraded.md", "no PipeWire / no audio devices"),
    DegradedCase("battery", ["battery", "togglePanel"], ["battery", "togglePanel"],
                 "panel_battery_degraded.md", "no battery"),
    DegradedCase("media", ["media", "toggle"], ["media", "toggle"],
                 "panel_media_degraded.md", "no media player"),
    DegradedCase("tray", ["tray", "togglePanel"], ["tray", "togglePanel"],
                 "panel_tray_degraded.md", "no tray items"),
]


@pytest.mark.cheat_aware(
    protects=(
        "panels degrade gracefully when their backing service is absent — they "
        "show an explicit empty/unavailable state, never a blank panel, stale "
        "data, or a crash"
    ),
    severity="high",
    cheats=[
        "weaken the judge so a blank/crashed panel scores PASS",
        "loosen the degraded golden until it matches any framebuffer",
        "turn a real degraded-state regression into skip/xfail",
    ],
    consequence=(
        "a panel could crash the shell or show fabricated data when its service "
        "is missing, and CI would stay green"
    ),
)
@pytest.mark.parametrize("case", DEGRADED_CASES, ids=lambda c: c.id)
def test_panel_degraded(vm_session, case):
    import time
    s = vm_session
    runner.ipc_vm(s, *case.open_cmd)
    time.sleep(1.2)
    png = runner.ARTIFACTS_DIR / f"panel_{case.id}_degraded.png"
    try:
        runner.screenshot_vm(s, png)
        actual = runner.describe(png)
        # Shell still alive (a panel that crashed the process fails this).
        runner.ipc_vm(s, "bar", "showBar")
    finally:
        try:
            runner.ipc_vm(s, *case.close_cmd)
            time.sleep(0.5)
        except Exception:
            pass

    assert png.exists()
    if not actual.strip():
        pytest.skip("no vision backend available to judge degraded state")
    reference = (runner.EXPECTATIONS_DIR / case.expectation).read_text()
    verdict = runner.judge(reference, actual)
    if verdict.verdict == "SKIP":
        pytest.skip(verdict.raw)
    assert verdict.verdict == "PASS", (
        f"panel '{case.id}' did not degrade gracefully under '{case.service}'.\n"
        f"  missing: {verdict.missing}\n  extra: {verdict.extra}\n"
        f"  judge: {verdict.raw}\n  png: {png}\n  actual:\n{actual}"
    )
