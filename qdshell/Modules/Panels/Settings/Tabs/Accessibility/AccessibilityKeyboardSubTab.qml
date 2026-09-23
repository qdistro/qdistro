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

  // Capability gate: when no xkb accessx backend is reachable these controls
  // are still shown (persist-only) but disabled with an explanation, following
  // the PowerService capability-gating pattern.
  readonly property bool backendOk: AccessibilityService.canApplyKeyboard

  NText {
    text: I18n.tr("panels.accessibility.keyboard-section")
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
    label: I18n.tr("panels.accessibility.sticky-keys-label")
    description: root.backendOk ? I18n.tr("panels.accessibility.sticky-keys-description") : I18n.tr("panels.accessibility.backend-unsupported-note")
    checked: Settings.data.accessibility.stickyKeys
    onToggled: checked => Settings.data.accessibility.stickyKeys = checked
    defaultValue: Settings.getDefaultValue("accessibility.stickyKeys")
  }

  NDivider {
    Layout.fillWidth: true
  }

  NToggle {
    Layout.fillWidth: true
    enabled: root.backendOk
    label: I18n.tr("panels.accessibility.slow-keys-label")
    description: root.backendOk ? I18n.tr("panels.accessibility.slow-keys-description") : I18n.tr("panels.accessibility.backend-unsupported-note")
    checked: Settings.data.accessibility.slowKeys
    onToggled: checked => Settings.data.accessibility.slowKeys = checked
    defaultValue: Settings.getDefaultValue("accessibility.slowKeys")
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: root.backendOk && Settings.data.accessibility.slowKeys
    label: I18n.tr("panels.accessibility.slow-keys-delay-label")
    minimum: 0
    maximum: 2000
    stepSize: 50
    suffix: " ms"
    value: Settings.data.accessibility.slowKeysDelayMs
    onValueChanged: Settings.data.accessibility.slowKeysDelayMs = value
    defaultValue: Settings.getDefaultValue("accessibility.slowKeysDelayMs")
  }

  NDivider {
    Layout.fillWidth: true
  }

  NToggle {
    Layout.fillWidth: true
    enabled: root.backendOk
    label: I18n.tr("panels.accessibility.bounce-keys-label")
    description: root.backendOk ? I18n.tr("panels.accessibility.bounce-keys-description") : I18n.tr("panels.accessibility.backend-unsupported-note")
    checked: Settings.data.accessibility.bounceKeys
    onToggled: checked => Settings.data.accessibility.bounceKeys = checked
    defaultValue: Settings.getDefaultValue("accessibility.bounceKeys")
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: root.backendOk && Settings.data.accessibility.bounceKeys
    label: I18n.tr("panels.accessibility.bounce-keys-delay-label")
    minimum: 0
    maximum: 2000
    stepSize: 50
    suffix: " ms"
    value: Settings.data.accessibility.bounceKeysDelayMs
    onValueChanged: Settings.data.accessibility.bounceKeysDelayMs = value
    defaultValue: Settings.getDefaultValue("accessibility.bounceKeysDelayMs")
  }
}
