import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Widgets

// Removable-media settings: insert/remove notifications, mount policy,
// and the AUTORUN policy. The autorun options are deliberately only
// ignore / prompt / open — there is no "run"/"execute" option anywhere,
// because qdshell never auto-executes anything off removable media. All
// mounting is brokered through the qdistro broker (qdistro-media-exec);
// this tab only sets the policy that decides whether to ask.
ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  NToggle {
    label: I18n.tr("panels.removable-media.enabled-label")
    description: I18n.tr("panels.removable-media.enabled-description")
    checked: Settings.data.removableMedia.enabled
    onToggled: checked => Settings.data.removableMedia.enabled = checked
    defaultValue: Settings.getDefaultValue("removableMedia.enabled")
  }

  NToggle {
    label: I18n.tr("panels.removable-media.notify-insert-label")
    description: I18n.tr("panels.removable-media.notify-insert-description")
    checked: Settings.data.removableMedia.notifyOnInsert
    onToggled: checked => Settings.data.removableMedia.notifyOnInsert = checked
    defaultValue: Settings.getDefaultValue("removableMedia.notifyOnInsert")
    enabled: Settings.data.removableMedia.enabled
  }

  NToggle {
    label: I18n.tr("panels.removable-media.notify-remove-label")
    description: I18n.tr("panels.removable-media.notify-remove-description")
    checked: Settings.data.removableMedia.notifyOnRemove
    onToggled: checked => Settings.data.removableMedia.notifyOnRemove = checked
    defaultValue: Settings.getDefaultValue("removableMedia.notifyOnRemove")
    enabled: Settings.data.removableMedia.enabled
  }

  NComboBox {
    label: I18n.tr("panels.removable-media.mount-policy-label")
    description: I18n.tr("panels.removable-media.mount-policy-description")
    model: [
      {
        "key": "manual",
        "name": I18n.tr("options.removable-media-mount.manual")
      },
      {
        "key": "prompt",
        "name": I18n.tr("options.removable-media-mount.prompt")
      }
    ]
    currentKey: Settings.data.removableMedia.mountPolicy
    defaultValue: Settings.getDefaultValue("removableMedia.mountPolicy")
    onSelected: key => Settings.data.removableMedia.mountPolicy = key
    enabled: Settings.data.removableMedia.enabled
  }

  NComboBox {
    label: I18n.tr("panels.removable-media.autorun-policy-label")
    description: I18n.tr("panels.removable-media.autorun-policy-description")
    model: [
      {
        "key": "ignore",
        "name": I18n.tr("options.removable-media-autorun.ignore")
      },
      {
        "key": "prompt",
        "name": I18n.tr("options.removable-media-autorun.prompt")
      },
      {
        "key": "open",
        "name": I18n.tr("options.removable-media-autorun.open")
      }
    ]
    currentKey: Settings.data.removableMedia.autorunPolicy
    defaultValue: Settings.getDefaultValue("removableMedia.autorunPolicy")
    onSelected: key => Settings.data.removableMedia.autorunPolicy = key
    enabled: Settings.data.removableMedia.enabled
  }

  NText {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    text: I18n.tr("panels.removable-media.security-note")
    textFormat: Text.PlainText
    wrapMode: Text.WordWrap
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
  }
}
