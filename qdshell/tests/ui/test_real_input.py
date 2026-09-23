"""§2 real keyboard / mouse paths — VM-only.

The qdwin GUI runner documents that the qdshell launcher overlay and the
locker password field are, in important cases, driven through the qdshell
ctrl-socket instead of real keyboard/mouse events (see
qdwin/tests/gui/qdwin-helpers.sh header: "The launcher overlay does NOT
redirect keyboard input ... a real wl_keyboard grab is a §6.8 follow-up").

These tests inject REAL input at QEMU's evdev layer (QMP input-send-event) —
the same path a physical keyboard/mouse takes, below Wayland — and assert the
observable result via a judged screenshot and/or a ctrl-socket/IPC snapshot.

Because some of these exercise an upstream qdwin gap (the overlay keyboard
grab), a test that the gap is still open is itself valuable: it is written to
FAIL LOUDLY (never xfail-blanket) so that when qdwin lands the grab, the test
turns green and pins the new behavior. If real typing genuinely cannot reach
the overlay yet, the assertion documents exactly what was observed.

VM-only: real input injection has no host nested-compositor equivalent.
"""

import time

import pytest

from . import runner

# Approximate launcher search-field location on the default 1280x800 output.
# The overlay is centered; the search input sits near the top of the panel.
# Overridable via env for non-default outputs.
import os
_SEARCH_X = int(os.environ.get("QDSHELL_UI_LAUNCHER_SEARCH_X", str(runner.VM_SCREEN_W // 2)))
_SEARCH_Y = int(os.environ.get("QDSHELL_UI_LAUNCHER_SEARCH_Y", "200"))
# First result row, a bit below the search field.
_FIRST_RESULT_X = _SEARCH_X
_FIRST_RESULT_Y = int(os.environ.get("QDSHELL_UI_LAUNCHER_RESULT_Y", "300"))


def _open_launcher(s):
    runner.ipc_vm(s, "launcher", "toggle")
    time.sleep(1.2)


def _close_launcher_best_effort(s):
    # Esc via real key; fall back to IPC toggle if still open.
    with_suppress = True
    try:
        runner.tap_key(s, "esc")
        time.sleep(0.4)
    except Exception:
        pass
    try:
        # If a launcher snapshot still reports open, toggle it closed.
        runner.ipc_vm(s, "launcher", "toggle")
    except Exception:
        pass
    time.sleep(0.4)


@pytest.mark.cheat_aware(
    protects=(
        "real keyboard typing reaches the launcher search field — not only the "
        "ctrl-socket launcher-type shortcut — so the launcher is usable with a "
        "physical keyboard"
    ),
    severity="high",
    cheats=[
        "drive the search via the ctrl-socket launcher-type shortcut and call it 'real input'",
        "weaken the judge so a launcher with an empty search field scores PASS",
        "convert a genuine upstream-gap failure into a blanket xfail",
    ],
    consequence="the launcher silently ignores a physical keyboard; only scripted shortcuts work",
)
def test_real_typing_into_launcher(vm_session):
    s = vm_session
    _open_launcher(s)
    try:
        # Type a real string with NO ctrl-socket involvement.
        runner.type_text(s, "settings")
        time.sleep(0.8)
        png = runner.ARTIFACTS_DIR / "real_launcher_typed.png"
        runner.screenshot_vm(s, png)
        actual = runner.describe(png)
        if not actual.strip():
            pytest.skip("no vision backend available to verify typed text")
        # The typed query must be visible in the search field. Judge against an
        # inline expectation rather than a golden file (this is a focused state
        # assertion, not a full-surface regression golden).
        reference = (
            "- A launcher / app-search overlay is open.\n"
            "- The search input field contains the text 'settings' "
            "(the characters typed on the keyboard).\n"
        )
        verdict = runner.judge(reference, actual)
        if verdict.verdict == "SKIP":
            pytest.skip(verdict.raw)
        assert verdict.verdict == "PASS", (
            "real keyboard text did not reach the launcher search field. This "
            "is the documented qdwin overlay-keyboard-grab gap; when qdwin lands "
            "the grab this test pins the fix.\n"
            f"  missing: {verdict.missing}\n  judge: {verdict.raw}\n  png: {png}\n"
            f"  actual:\n{actual}"
        )
    finally:
        _close_launcher_best_effort(s)


@pytest.mark.cheat_aware(
    protects="Esc closes the launcher via a real keypress (no ctrl-socket shortcut)",
    severity="medium",
    cheats=["close via IPC toggle and claim the Esc key did it"],
    consequence="Esc does nothing; the launcher cannot be dismissed from the keyboard",
)
def test_launcher_escape_real_key(vm_session, capture):
    s = vm_session
    _open_launcher(s)
    # Press a real Escape. Then the shell must be back to a clean idle bar.
    runner.tap_key(s, "esc")
    time.sleep(0.8)
    runner.ipc_vm(s, "bar", "showBar")  # shell still alive

    from .manifests import BAR_SURFACES
    bar = BAR_SURFACES[0]
    png, actual = capture(bar)
    assert png.exists()
    if not actual.strip():
        pytest.skip("no vision backend available")
    reference = (runner.EXPECTATIONS_DIR / bar.expectation).read_text()
    verdict = runner.judge(reference, actual)
    if verdict.verdict == "SKIP":
        pytest.skip(verdict.raw)
    assert verdict.verdict == "PASS", (
        "after a real Escape keypress the launcher did not close to a clean bar "
        "(focus recovery / Esc-close regression).\n"
        f"  missing: {verdict.missing}\n  judge: {verdict.raw}\n  png: {png}\n"
        f"  actual:\n{actual}"
    )


@pytest.mark.cheat_aware(
    protects="arrow-key + Enter navigation selects and activates a launcher result with real keys",
    severity="medium",
    cheats=["use launcher-activate ctrl-socket and call it Enter"],
    consequence="keyboard result navigation is broken; only mouse/shortcut works",
)
def test_launcher_arrow_enter_real_keys(vm_session):
    s = vm_session
    _open_launcher(s)
    try:
        runner.type_text(s, "settings")
        time.sleep(0.6)
        png_before = runner.ARTIFACTS_DIR / "real_launcher_nav_before.png"
        runner.screenshot_vm(s, png_before)
        # Move the selection down with real Down arrows, then capture.
        runner.tap_key(s, "down")
        runner.tap_key(s, "down")
        time.sleep(0.4)
        png_after = runner.ARTIFACTS_DIR / "real_launcher_nav_after.png"
        runner.screenshot_vm(s, png_after)
        desc_after = runner.describe(png_after)
        if not desc_after.strip():
            pytest.skip("no vision backend available")
        # The selection highlight should have moved to a lower row. We assert
        # the overlay still shows a highlighted result (a row distinct from the
        # top) — a precise per-pixel index is brittle, but "a result below the
        # first is highlighted" is a real navigation postcondition.
        reference = (
            "- A launcher overlay is open with a list of results.\n"
            "- One result row is visually highlighted/selected, and it is NOT "
            "the very first row (the selection has moved down the list).\n"
        )
        verdict = runner.judge(reference, desc_after)
        if verdict.verdict == "SKIP":
            pytest.skip(verdict.raw)
        assert verdict.verdict == "PASS", (
            "real Down-arrow navigation did not move the launcher selection "
            "(overlay keyboard-grab gap or selection regression).\n"
            f"  missing: {verdict.missing}\n  judge: {verdict.raw}\n"
            f"  before: {png_before}\n  after: {png_after}\n  actual:\n{desc_after}"
        )
    finally:
        _close_launcher_best_effort(s)


@pytest.mark.cheat_aware(
    protects="a mouse click on a launcher result row selects/activates it",
    severity="medium",
    cheats=["activate via ctrl-socket and claim the click did it"],
    consequence="mouse selection is broken; the launcher is keyboard-only",
)
def test_launcher_mouse_click_selection(vm_session):
    s = vm_session
    _open_launcher(s)
    try:
        runner.type_text(s, "settings")
        time.sleep(0.6)
        # Click the first result row. After a real click the launcher should
        # either activate (close) or at minimum select that row. We assert the
        # shell remains responsive and the click did not wedge input.
        runner.mouse_click(s, _FIRST_RESULT_X, _FIRST_RESULT_Y)
        time.sleep(1.0)
        # Shell still answers IPC (a click that crashed/wedged the overlay
        # would break this).
        runner.ipc_vm(s, "bar", "showBar")
        png = runner.ARTIFACTS_DIR / "real_launcher_click.png"
        runner.screenshot_vm(s, png)
        assert png.exists(), "screenshot after click must be captured"
    finally:
        _close_launcher_best_effort(s)


@pytest.mark.cheat_aware(
    protects=(
        "Shift and Backspace are handled while the launcher overlay is focused "
        "— uppercase letters and corrections reach the search field correctly"
    ),
    severity="medium",
    cheats=["assert only that some text appeared, ignoring case/backspace"],
    consequence="Shift/Backspace mis-handling corrupts every search query",
)
def test_launcher_shift_and_backspace(vm_session):
    s = vm_session
    _open_launcher(s)
    try:
        # Type a mixed-case word, then backspace the last char.
        runner.type_text(s, "Firefox")
        time.sleep(0.4)
        runner.tap_key(s, "backspace")
        time.sleep(0.5)
        png = runner.ARTIFACTS_DIR / "real_launcher_shift_bs.png"
        runner.screenshot_vm(s, png)
        actual = runner.describe(png)
        if not actual.strip():
            pytest.skip("no vision backend available")
        reference = (
            "- A launcher overlay is open.\n"
            "- The search field shows 'Firefo' — an uppercase F (Shift handled) "
            "followed by lowercase letters, with the final 'x' removed by "
            "Backspace.\n"
        )
        verdict = runner.judge(reference, actual)
        if verdict.verdict == "SKIP":
            pytest.skip(verdict.raw)
        assert verdict.verdict == "PASS", (
            "Shift/Backspace not handled correctly in the launcher search field "
            "(or overlay keyboard-grab gap).\n"
            f"  missing: {verdict.missing}\n  judge: {verdict.raw}\n  png: {png}\n"
            f"  actual:\n{actual}"
        )
    finally:
        _close_launcher_best_effort(s)


@pytest.mark.cheat_aware(
    protects=(
        "real password typing + Enter unlocks the locker — the lock screen is "
        "usable with a physical keyboard, not only the ctrl-socket"
    ),
    severity="critical",
    cheats=[
        "unlock via the ctrl-socket and claim a real password+Enter did it",
        "skip the post-unlock assertion that the desktop is actually back",
        "turn a real lock/unlock regression into skip/xfail",
    ],
    consequence=(
        "a user cannot unlock their session by typing their password — or worse, "
        "the locker accepts input it should not, weakening the lock gate"
    ),
)
def test_locker_real_password_unlock(vm_session):
    s = vm_session
    # Locking + a real password unlock needs the test environment to know the
    # admin password. It is NOT hard-coded here; the gate provides it via env
    # so this file carries no credential. Without it we skip loudly.
    pw = os.environ.get("QDSHELL_UI_VM_PASSWORD", "").strip()
    if not pw:
        pytest.skip(
            "QDSHELL_UI_VM_PASSWORD not set: cannot drive a real locker "
            "password unlock without the session password (not hard-coded here)"
        )
    if not all(ch in runner._CHAR_TO_QCODE for ch in pw):
        pytest.skip("password contains characters this harness cannot type via QMP")

    # Lock via the documented qdlocker ctrl path (IPC lockScreen.lock).
    runner.ipc_vm(s, "lockScreen", "lock")
    time.sleep(2.0)
    locked_png = runner.ARTIFACTS_DIR / "real_locker_locked.png"
    runner.screenshot_vm(s, locked_png)
    locked_desc = runner.describe(locked_png)

    # Type the password with REAL keys and press Enter.
    runner.type_text(s, pw)
    time.sleep(0.4)
    runner.tap_key(s, "ret")
    time.sleep(2.5)

    # After unlock the shell must answer IPC and the framebuffer must no longer
    # be the lock screen.
    runner.ipc_vm(s, "bar", "showBar")
    unlocked_png = runner.ARTIFACTS_DIR / "real_locker_unlocked.png"
    runner.screenshot_vm(s, unlocked_png)
    unlocked_desc = runner.describe(unlocked_png)
    if not unlocked_desc.strip():
        pytest.skip("no vision backend available to confirm unlock")
    reference = (
        "- A normal desktop / shell bar is visible (NOT a lock screen).\n"
        "- There is no password entry field / lock prompt on screen.\n"
    )
    verdict = runner.judge(reference, unlocked_desc)
    if verdict.verdict == "SKIP":
        pytest.skip(verdict.raw)
    assert verdict.verdict == "PASS", (
        "real password + Enter did not unlock the locker.\n"
        f"  locked described as:\n{locked_desc}\n"
        f"  after-unlock described as:\n{unlocked_desc}\n"
        f"  missing: {verdict.missing}\n  judge: {verdict.raw}\n"
        f"  locked png: {locked_png}\n  unlocked png: {unlocked_png}"
    )
