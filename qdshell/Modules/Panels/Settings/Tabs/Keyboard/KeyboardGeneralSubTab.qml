import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.Keyboard
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  width: parent.width

  // ─── Capability note ─────────────────────────────────────────────
  // Mirrors PowerService gating: when no live apply backend exists we tell
  // the user settings are persisted only.
  Rectangle {
    Layout.fillWidth: true
    visible: KeyboardInputService.persistOnly
    color: Color.mSurfaceVariant
    radius: Style.iRadiusM
    implicitHeight: noteLayout.implicitHeight + Style.marginM * 2

    RowLayout {
      id: noteLayout
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      NIcon {
        icon: "info"
        color: Color.mPrimary
        pointSize: Style.fontSizeL
        Layout.alignment: Qt.AlignTop
      }
      NText {
        Layout.fillWidth: true
        text: I18n.tr("panels.keyboard.capability-note")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.WordWrap
      }
    }
  }

  // ─── Use system defaults ─────────────────────────────────────────
  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.use-system-defaults-label")
    description: I18n.tr("panels.keyboard.use-system-defaults-description")
    checked: Settings.data.keyboard.useSystemDefaults
    onToggled: checked => Settings.data.keyboard.useSystemDefaults = checked
    defaultValue: Settings.getDefaultValue("keyboard.useSystemDefaults")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Typing — key repeat
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
    Layout.bottomMargin: Style.marginS
  }

  NText {
    text: I18n.tr("panels.keyboard.section-typing")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  // Key repeat / cursor blink are behavior settings; they are NOT scoped by
  // "use system defaults" (which, per XFCE, only governs the layout block).
  NSpinBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.repeat-delay-label")
    description: I18n.tr("panels.keyboard.repeat-delay-description")
    minimum: 100
    maximum: 2000
    stepSize: 50
    suffix: " ms"
    value: Settings.data.keyboard.repeatDelay
    onValueChanged: Settings.data.keyboard.repeatDelay = value
    defaultValue: Settings.getDefaultValue("keyboard.repeatDelay")
  }

  NSpinBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.repeat-rate-label")
    description: I18n.tr("panels.keyboard.repeat-rate-description")
    minimum: 1
    maximum: 110
    stepSize: 1
    suffix: " Hz"
    value: Settings.data.keyboard.repeatRate
    onValueChanged: Settings.data.keyboard.repeatRate = value
    defaultValue: Settings.getDefaultValue("keyboard.repeatRate")
  }

  // ─── Test area ───────────────────────────────────────────────────
  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.test-area-label")
    description: I18n.tr("panels.keyboard.test-area-description")
    placeholderText: I18n.tr("panels.keyboard.test-area-placeholder")
    text: ""
  }

  // ═══════════════════════════════════════════════════════════════════
  // Cursor blink
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
    Layout.bottomMargin: Style.marginS
  }

  NText {
    text: I18n.tr("panels.keyboard.section-cursor")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.keyboard.cursor-persist-note")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.cursor-blink-label")
    description: I18n.tr("panels.keyboard.cursor-blink-description")
    checked: Settings.data.keyboard.cursorBlink
    onToggled: checked => Settings.data.keyboard.cursorBlink = checked
    defaultValue: Settings.getDefaultValue("keyboard.cursorBlink")
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: Settings.data.keyboard.cursorBlink
    label: I18n.tr("panels.keyboard.cursor-blink-rate-label")
    description: I18n.tr("panels.keyboard.cursor-blink-rate-description")
    minimum: 200
    maximum: 3000
    stepSize: 100
    suffix: " ms"
    value: Settings.data.keyboard.cursorBlinkRate
    onValueChanged: Settings.data.keyboard.cursorBlinkRate = value
    defaultValue: Settings.getDefaultValue("keyboard.cursorBlinkRate")
  }

  // ═══════════════════════════════════════════════════════════════════
  // NumLock
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginS
    Layout.bottomMargin: Style.marginS
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.keyboard.restore-numlock-label")
    description: I18n.tr("panels.keyboard.restore-numlock-description")
    checked: Settings.data.keyboard.restoreNumLock
    onToggled: checked => Settings.data.keyboard.restoreNumLock = checked
    defaultValue: Settings.getDefaultValue("keyboard.restoreNumLock")
  }
}
