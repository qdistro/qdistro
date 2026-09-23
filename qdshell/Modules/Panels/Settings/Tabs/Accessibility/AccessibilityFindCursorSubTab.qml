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

  property var screen

  readonly property bool enabledNow: Settings.data.accessibility.findCursorEnabled

  NText {
    text: I18n.tr("panels.accessibility.find-cursor-section")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.accessibility.find-cursor-enable-label")
    description: I18n.tr("panels.accessibility.find-cursor-enable-description")
    checked: Settings.data.accessibility.findCursorEnabled
    onToggled: checked => Settings.data.accessibility.findCursorEnabled = checked
    defaultValue: Settings.getDefaultValue("accessibility.findCursorEnabled")
  }

  // The trigger is a compositor-level global shortcut, which qdshell cannot
  // register itself from here. Surface the IPC command (read-only) so the user
  // can bind it in their compositor config; we still persist their chosen
  // shortcut string as a reminder/note of what they bound it to.
  NTextInput {
    Layout.fillWidth: true
    readOnly: true
    label: I18n.tr("panels.accessibility.find-cursor-shortcut-label")
    description: I18n.tr("panels.accessibility.find-cursor-shortcut-description")
    text: "qs ipc call findCursor show"
  }

  NTextInput {
    Layout.fillWidth: true
    enabled: root.enabledNow
    label: I18n.tr("panels.accessibility.find-cursor-shortcut-note-label")
    description: I18n.tr("panels.accessibility.find-cursor-shortcut-note-description")
    text: Settings.data.accessibility.findCursorShortcut
    settingsPath: "accessibility.findCursorShortcut"
    defaultValue: Settings.getDefaultValue("accessibility.findCursorShortcut")
    onEditingFinished: Settings.data.accessibility.findCursorShortcut = text
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM

    NText {
      Layout.fillWidth: true
      text: I18n.tr("panels.accessibility.find-cursor-test-label")
      color: Color.mOnSurfaceVariant
      pointSize: Style.fontSizeS
      wrapMode: Text.WordWrap
    }

    NButton {
      text: I18n.tr("panels.accessibility.find-cursor-test-button")
      icon: "accessible"
      enabled: root.enabledNow
      onClicked: AccessibilityService.previewFindCursor()
    }
  }

  NDivider {
    Layout.fillWidth: true
  }

  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM
    enabled: root.enabledNow

    NLabel {
      Layout.fillWidth: true
      label: I18n.tr("panels.accessibility.find-cursor-ring-color-label")
    }

    NColorPicker {
      screen: root.screen
      Layout.preferredWidth: Style.sliderWidth
      Layout.preferredHeight: Style.baseWidgetSize
      selectedColor: Settings.data.accessibility.findCursorRingColor || "#ff4081"
      onColorSelected: color => Settings.data.accessibility.findCursorRingColor = color
    }
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: root.enabledNow
    label: I18n.tr("panels.accessibility.find-cursor-ring-size-label")
    description: I18n.tr("panels.accessibility.find-cursor-ring-size-description")
    minimum: 60
    maximum: 600
    stepSize: 10
    suffix: " px"
    value: Settings.data.accessibility.findCursorRingSize
    onValueChanged: Settings.data.accessibility.findCursorRingSize = value
    defaultValue: Settings.getDefaultValue("accessibility.findCursorRingSize")
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: root.enabledNow
    label: I18n.tr("panels.accessibility.find-cursor-duration-label")
    description: I18n.tr("panels.accessibility.find-cursor-duration-description")
    minimum: 150
    maximum: 3000
    stepSize: 50
    suffix: " ms"
    value: Settings.data.accessibility.findCursorDurationMs
    onValueChanged: Settings.data.accessibility.findCursorDurationMs = value
    defaultValue: Settings.getDefaultValue("accessibility.findCursorDurationMs")
  }
}
