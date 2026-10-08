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

from . import fixtures, runner


@dataclass(frozen=True)
class DegradedCase:
    id: str
    open_cmd: list
    close_cmd: list
    expectation: str
    service: str
    # Guest bash snippets (runner.guest_sh_vm): setup runs before the panel
    # opens, teardown runs in finally after it closes. Used to INDUCE the
    # degraded condition on VMs that aren't naturally degraded.
    setup_guest: tuple = ()
    teardown_guest: tuple = ()


DEGRADED_CASES = [
    DegradedCase("bluetooth", ["bluetooth", "togglePanel"], ["bluetooth", "togglePanel"],
                 "panel_bluetooth_degraded.md", "no Bluetooth adapter / BT off"),
    DegradedCase("network", ["network", "togglePanel"], ["network", "togglePanel"],
                 "panel_network_degraded.md", "offline network"),
    # qdwin VMs ship an hda codec + PipeWire, so audio is NOT naturally
    # degraded here — the absence is induced (and restored) per case.
    DegradedCase("audio", ["audio", "togglePanel"], ["audio", "togglePanel"],
                 "panel_audio_degraded.md", "no PipeWire / no audio devices",
                 setup_guest=(fixtures.AUDIO_DEGRADE,),
                 teardown_guest=(fixtures.AUDIO_RESTORE,)),
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
    png = runner.ARTIFACTS_DIR / f"panel_{case.id}_degraded.png"
    teardown_errs = []
    try:
        for cmd in case.setup_guest:
            res = runner.guest_sh_vm(s, cmd)
            assert res.returncode == 0, (
                f"degraded-state inducement for '{case.id}' failed "
                f"(rc={res.returncode}): {res.stderr.strip()[:300]}"
            )
        runner.ipc_vm(s, *case.open_cmd)
        # Panels that auto-close on empty (TrayDrawerPanel) animate shut; the
        # capture must outlast the transition or it judges a half-rendered frame.
        time.sleep(2.5)
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
        for cmd in case.teardown_guest:
            res = runner.guest_cleanup_vm(s, cmd)
            if res.returncode != 0:
                teardown_errs.append(
                    f"rc={res.returncode}: {res.stderr.strip()[:200]}")
        # A failed restore is a failure, not a warning: leftover induced
        # state silently contaminates every later case. Raised inside the
        # finally so a capture failure still chains as __context__ rather
        # than the restore error being skipped when the body raised.
        if teardown_errs:
            raise RuntimeError(
                f"degraded teardown for '{case.id}' failed: "
                + "; ".join(teardown_errs)
            )

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
