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

  readonly property bool backendOk: AccessibilityService.keyboardBackendAvailable

  NText {
    text: I18n.tr("panels.accessibility.mouse-keys-section")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    visible: !root.backendOk
    text: I18n.tr("panels.accessibility.keyboard-backend-unavailable")
    color: Color.mError
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }

  NToggle {
    Layout.fillWidth: true
    enabled: root.backendOk
    label: I18n.tr("panels.accessibility.mouse-keys-label")
    description: root.backendOk ? I18n.tr("panels.accessibility.mouse-keys-description") : I18n.tr("panels.accessibility.backend-unsupported-note")
    checked: Settings.data.accessibility.mouseKeys
    onToggled: checked => Settings.data.accessibility.mouseKeys = checked
    defaultValue: Settings.getDefaultValue("accessibility.mouseKeys")
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: root.backendOk && Settings.data.accessibility.mouseKeys
    label: I18n.tr("panels.accessibility.mouse-keys-speed-label")
    description: I18n.tr("panels.accessibility.mouse-keys-speed-description")
    minimum: 1
    maximum: 100
    stepSize: 1
    value: Settings.data.accessibility.mouseKeysSpeed
    onValueChanged: Settings.data.accessibility.mouseKeysSpeed = value
    defaultValue: Settings.getDefaultValue("accessibility.mouseKeysSpeed")
  }
}
