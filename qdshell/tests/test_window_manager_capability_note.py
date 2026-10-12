"""Window-manager status copy must agree with the capability-gated UI."""

import json
from pathlib import Path

QDSHELL_ROOT = Path(__file__).resolve().parents[1]


def test_window_manager_banner_and_shortcut_note_follow_capabilities():
    tab = (QDSHELL_ROOT / "Modules/Panels/Settings/Tabs/WindowManager"
           / "WindowManagerTab.qml").read_text()
    service = (QDSHELL_ROOT / "Services/Qdwin/WindowManagerService.qml").read_text()
    translations = json.loads(
        (QDSHELL_ROOT / "Assets/Translations/en.json").read_text())
    note = translations["panels"]["window-manager"]["shortcuts-note"]

    # The absent banner on a capable qdwin is intentional, even while the
    # shortcut note remains visible in the same Settings tab.
    assert "visible: !WindowManagerService.canApplyWmPolicy" in tab
    assert "canRegisterShortcuts: CapabilityService.keybindRegistration" in service
    assert "when qdwin supports global keybinds" in note
    assert "otherwise they remain saved" in note
    assert "once qdwin supports window-manager policy" not in note
