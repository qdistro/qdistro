"""Pin settings properties that cannot be read from a static screenshot."""

import json
from pathlib import Path

QDSHELL_ROOT = Path(__file__).resolve().parents[1]


def test_notification_minimum_width_control_range_and_binding():
    source = (QDSHELL_ROOT / "Modules/Panels/Settings/Tabs/Notifications"
              / "GeneralSubTab.qml").read_text()
    label = 'label: I18n.tr("panels.notifications.settings-min-width-label")'
    assert label in source
    control = source.split(label, 1)[1].split("}", 1)[0]
    assert "minimum: 200" in control
    assert "maximum: 1200" in control
    assert 'suffix: " px"' in control
    assert "value: Settings.data.notifications.minWidth" in control
    assert "onValueChanged: Settings.data.notifications.minWidth = value" in control


def test_appearance_gtk_theme_description_matches_scanner_roots():
    source = (QDSHELL_ROOT / "Modules/Panels/Settings/Tabs/Appearance"
              / "AppearanceTab.qml").read_text()
    translations = json.loads(
        (QDSHELL_ROOT / "Assets/Translations/en.json").read_text())
    description = translations["panels"]["appearance"]["gtk-theme-description"]
    for root in ("~/.themes", "~/.local/share/themes", "/usr/share/themes"):
        assert root in description
        assert root.replace("~", "$HOME") in source
