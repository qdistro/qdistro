"""Visual checks for settings controls hidden behind a second interaction."""

import os
import time

from PIL import Image

from . import runner


def _require_gui_profile(png):
    """Reject a production-mode VM before calibrated pointer clicks."""
    with Image.open(png) as frame:
        actual = frame.size
    expected = (runner.VM_SCREEN_W, runner.VM_SCREEN_H)
    assert actual == expected, (
        f"GUI VM screen is {actual}, expected {expected}; use "
        "spin-test-vm-gui.sh with QDISTRO_VM_GUI_SESSION=qdwin")


def test_desktopicons_sort_options(vm_session):
    """The closed default-off chooser cannot prove its hidden Type option."""
    s = vm_session
    settings = "/home/admin/.config/qdshell/settings.json"
    backup = f"/tmp/qdshell-ui-desktopicons-{os.getpid()}.json"
    save = runner.guest_sh_vm_raw(
        s, f"set -e\nif [ -f {settings} ]; then cp -p {settings} {backup}; "
        "echo present; else echo absent; fi\n")
    assert save.returncode == 0, f"could not back up settings: {save.stderr}"
    original = save.stdout.strip()
    assert original in ("present", "absent"), save.stdout

    try:
        runner.ipc_vm(s, "settings", "openTab", "desktopicons")
        time.sleep(1.2)
        default_png = runner.ARTIFACTS_DIR / "settings_desktopicons_default.png"
        runner.screenshot_vm(s, default_png)
        _require_gui_profile(default_png)
        master_state = runner._run_codex(
            'Inspect only the switch next to "Enable desktop icons" near '
            'pixel (1010,135) in this 1280x800 screenshot. State whether its '
            'circular thumb is left or right within the track; left means '
            'OFF and right means ON. Also state whether the dependent rows '
            'look dimmed. Do not infer from color. Reply exactly OFF_DIMMED '
            'only when the thumb is left and dependent rows are dimmed; '
            'otherwise reply exactly ON_OR_UNCLEAR.', default_png)
        assert master_state.strip() == "OFF_DIMMED", (
            f"Desktop Icons did not start disabled: {master_state!r}; "
            f"png={default_png}")
        # 1280x800 qdwin golden: enable switch thumb at (1002, 135), Sort
        # arrow at (1016, 296). Dependent controls must enable immediately.
        runner.mouse_click(s, int(runner.VM_SCREEN_W * 0.783),
                           int(runner.VM_SCREEN_H * 0.169))
        time.sleep(0.8)
        enabled_png = runner.ARTIFACTS_DIR / "settings_desktopicons_enabled.png"
        runner.screenshot_vm(s, enabled_png)
        enabled_state = runner._run_codex(
            'Inspect the switch next to "Enable desktop icons" near pixel '
            '(1010,135) in this 1280x800 screenshot. Reply exactly '
            'ON_ACTIVE only if its circular thumb is on the right and the '
            'dependent rows, including Sort by, are visibly active. '
            'Otherwise reply exactly OFF_OR_DIMMED. Do not infer switch '
            'state from color alone.', enabled_png)
        assert enabled_state.strip() == "ON_ACTIVE", (
            f"Desktop Icons controls did not enable immediately: "
            f"{enabled_state!r}; png={enabled_png}")
        runner.mouse_click(s, int(runner.VM_SCREEN_W * 0.794),
                           int(runner.VM_SCREEN_H * 0.370))
        time.sleep(0.8)
        png = runner.ARTIFACTS_DIR / "settings_desktopicons_sort_open.png"
        runner.screenshot_vm(s, png)
        actual = runner.describe(png)
        assert actual.strip(), f"no visual description for {png}"
        reference = (runner.EXPECTATIONS_DIR /
                     "settings_desktopicons_sort_open.md").read_text()
        verdict = runner.judge(reference, actual)
        assert verdict.verdict == "PASS", (
            f"Desktop Icons sort options not visible: {verdict.raw}\n"
            f"missing={verdict.missing}\npng={png}\nactual={actual}")
    finally:
        # UI toggles persist asynchronously. Stop the shell before restoring
        # the byte-for-byte original file so a late save cannot overwrite it.
        # The restart is required even after a failed capture: later UI cases
        # must never inherit desktop icons left enabled by this test.
        restore = runner.guest_cleanup_vm(
            s, "set -e\n"
            "trap 'systemctl --user start qdshell.service' EXIT\n"
            "systemctl --user stop qdshell.service\n"
            + (f"cp -p {backup} {settings}\n" if original == "present"
               else f"rm -f {settings}\n")
            + f"rm -f {backup}\n"
            "systemctl --user start qdshell.service\n"
            "trap - EXIT\n")
        if restore.returncode != 0:
            raise RuntimeError(f"desktop-icons settings restore failed: "
                               f"{restore.stderr.strip()}")
        runner._await_qdwin_binding_vm(s, "")


def test_bar_launcher_widget_editor(vm_session):
    """Visit the second view described by the Bar expectation explicitly."""
    s = vm_session
    runner.ipc_vm(s, "settings", "openTab", "bar")
    try:
        time.sleep(1.2)
        start_png = runner.ARTIFACTS_DIR / "settings_bar_start.png"
        runner.screenshot_vm(s, start_png)
        _require_gui_profile(start_png)
        # 1280x800 qdwin golden: Widgets subtab, then the settings gear on
        # the first (Launcher) widget in the Left section.
        runner.mouse_click(s, int(runner.VM_SCREEN_W * 0.530),
                           int(runner.VM_SCREEN_H * 0.155))
        time.sleep(2.5)
        widgets_png = runner.ARTIFACTS_DIR / "settings_bar_widgets.png"
        runner.screenshot_vm(s, widgets_png)
        widgets_actual = runner.describe(widgets_png)
        widgets_reference = (
            "- The Bar settings Widgets subtab is selected.\n"
            "- The Left section includes a Launcher widget with a settings "
            "button.\n")
        widgets_verdict = runner.judge(widgets_reference, widgets_actual)
        assert widgets_verdict.verdict == "PASS", (
            f"Launcher widget not visible in Widgets subtab: "
            f"{widgets_verdict.raw}\npng={widgets_png}\n"
            f"actual={widgets_actual}")

        runner.mouse_click(s, int(runner.VM_SCREEN_W * 0.498),
                           int(runner.VM_SCREEN_H * 0.366))
        time.sleep(1.5)
        png = runner.ARTIFACTS_DIR / "settings_bar_launcher_editor.png"
        runner.screenshot_vm(s, png)
        actual = runner.describe(png)
        # Dialog content can extend below 800px; preserve the golden's full
        # editor coverage by including every scrolled viewport.
        from types import SimpleNamespace
        actual = runner._describe_scrolled_vm(
            s, SimpleNamespace(id="settings_bar_launcher_editor"), png, actual)
        reference = (runner.EXPECTATIONS_DIR /
                     "settings_bar_launcher_editor.md").read_text()
        verdict = runner.judge(reference, actual)
        assert verdict.verdict == "PASS", (
            f"Launcher widget editor regressed: {verdict.raw}\n"
            f"missing={verdict.missing}\npng={png}\nactual={actual}")
    finally:
        try:
            runner.tap_key(s, "esc")  # close modal without editing settings
            runner.ipc_vm(s, "settings", "toggle")
        except Exception:
            pass
