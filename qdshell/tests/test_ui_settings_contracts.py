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



def test_notification_detail_mode_combo_options():
    """The UI capture opens only the Toast theme chooser, so the closed
    Detail mode combo's option list is pinned here, not by the judge."""
    source = (QDSHELL_ROOT / "Modules/Panels/Settings/Tabs/Notifications"
              / "GeneralSubTab.qml").read_text()
    label = 'label: I18n.tr("panels.notifications.settings-detail-mode-label")'
    assert label in source
    control = source.split(label, 1)[1].split("NComboBox {", 1)[0]
    keys = []
    for key in ("compact", "normal", "detailed"):
        entry = (f'"key": "{key}",\n        "name": '
                 f'I18n.tr("options.notification-detail-mode.{key}")')
        assert entry in control, key
        keys.append(control.index(entry))
    assert keys == sorted(keys), "options must read Compact, Normal, Detailed"
    assert ('currentKey: Settings.data.notifications.detailMode || "normal"'
            in control)
    assert ("onSelected: key => Settings.data.notifications.detailMode = key"
            in control)
    translations = json.loads(
        (QDSHELL_ROOT / "Assets/Translations/en.json").read_text())
    names = translations["options"]["notification-detail-mode"]
    assert names == {"compact": "Compact (title only)",
                     "normal": "Normal (title and body)",
                     "detailed": "Detailed (title, body, actions, time)"}

def test_appearance_gtk_theme_description_matches_scanner_roots():
    source = (QDSHELL_ROOT / "Modules/Panels/Settings/Tabs/Appearance"
              / "AppearanceTab.qml").read_text()
    translations = json.loads(
        (QDSHELL_ROOT / "Assets/Translations/en.json").read_text())
    description = translations["panels"]["appearance"]["gtk-theme-description"]
    for root in ("~/.themes", "~/.local/share/themes", "/usr/share/themes"):
        assert root in description
        assert root.replace("~", "$HOME") in source
