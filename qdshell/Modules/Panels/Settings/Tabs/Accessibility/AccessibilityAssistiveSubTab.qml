import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  width: parent.width

  readonly property bool backendOk: AccessibilityService.assistiveTechBackendAvailable

  NText {
    text: I18n.tr("panels.accessibility.assistive-section")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    visible: !root.backendOk
    text: I18n.tr("panels.accessibility.assistive-backend-unavailable")
    color: Color.mError
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }

  NToggle {
    Layout.fillWidth: true
    enabled: root.backendOk
    label: I18n.tr("panels.accessibility.assistive-autostart-label")
    description: root.backendOk ? I18n.tr("panels.accessibility.assistive-autostart-description") : I18n.tr("panels.accessibility.backend-unsupported-note")
    checked: Settings.data.accessibility.assistiveTechEnabled
    onToggled: checked => Settings.data.accessibility.assistiveTechEnabled = checked
    defaultValue: Settings.getDefaultValue("accessibility.assistiveTechEnabled")
  }
}
