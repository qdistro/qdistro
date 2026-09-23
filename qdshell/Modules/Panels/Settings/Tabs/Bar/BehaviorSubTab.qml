import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  readonly property string effectiveWheelAction: {
    if (Settings.data.bar.mouseWheelAction !== undefined && Settings.data.bar.mouseWheelAction !== "")
      return Settings.data.bar.mouseWheelAction;
    return Settings.data.bar.enableWorkspaceScroll ? "workspace" : "none";
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.bar.behavior-workspace-scroll-label")
    description: I18n.tr("panels.bar.behavior-workspace-scroll-description")
    model: [
      {
        "key": "none",
        "name": "Nothing"
      },
      {
        "key": "workspace",
        "name": "Workspace"
      }
    ]
    currentKey: root.effectiveWheelAction
    defaultValue: Settings.getDefaultValue("bar.mouseWheelAction")
    onSelected: key => {
                  Settings.data.bar.mouseWheelAction = key;
                  Settings.data.bar.enableWorkspaceScroll = (key === "workspace");
                }
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.general.reverse-scrolling-label")
    description: I18n.tr("panels.general.reverse-scrolling-description")
    checked: Settings.data.bar.reverseScroll
    defaultValue: Settings.getDefaultValue("bar.reverseScroll")
    onToggled: checked => Settings.data.bar.reverseScroll = checked
    visible: Settings.data.bar.mouseWheelAction !== "none"
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.bar.behavior-wheel-wrap-label")
    description: I18n.tr("panels.bar.behavior-wheel-wrap-description")
    checked: Settings.data.bar.mouseWheelWrap
    defaultValue: Settings.getDefaultValue("bar.mouseWheelWrap")
    onToggled: checked => Settings.data.bar.mouseWheelWrap = checked
    visible: Settings.data.bar.mouseWheelAction === "workspace"
  }
}
