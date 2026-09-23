import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.Qdwin
import qs.Services.UI
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // ─── Helper models ───────────────────────────────────────────────
  readonly property var focusPolicyModel: [
    {
      "key": "click",
      "name": I18n.tr("panels.window-manager.focus-policy-click")
    },
    {
      "key": "follow-mouse",
      "name": I18n.tr("panels.window-manager.focus-policy-follow-mouse")
    }
  ]

  readonly property var placementModel: [
    {
      "key": "center",
      "name": I18n.tr("panels.window-manager.placement-center")
    },
    {
      "key": "under-mouse",
      "name": I18n.tr("panels.window-manager.placement-under-mouse")
    },
    {
      "key": "smart",
      "name": I18n.tr("panels.window-manager.placement-smart")
    },
    {
      "key": "cascade",
      "name": I18n.tr("panels.window-manager.placement-cascade")
    }
  ]

  readonly property var titlebarActionModel: [
    {
      "key": "maximize",
      "name": I18n.tr("panels.window-manager.titlebar-action-maximize")
    },
    {
      "key": "shade",
      "name": I18n.tr("panels.window-manager.titlebar-action-shade")
    },
    {
      "key": "minimize",
      "name": I18n.tr("panels.window-manager.titlebar-action-minimize")
    },
    {
      "key": "nothing",
      "name": I18n.tr("panels.window-manager.titlebar-action-nothing")
    }
  ]

  readonly property bool ffmActive: Settings.data.windowManager.focusPolicy === "follow-mouse"

  // ═══════════════════════════════════════════════════════════════════
  // Backend status banner (capability gating, PowerService/Mouse-style)
  // ═══════════════════════════════════════════════════════════════════
  Rectangle {
    Layout.fillWidth: true
    visible: !WindowManagerService.canApplyWmPolicy
    radius: Style.iRadiusS
    color: Color.mSurfaceVariant
    border.color: Color.mOutline
    border.width: Style.borderS
    implicitHeight: backendRow.implicitHeight + Style.marginM * 2

    RowLayout {
      id: backendRow
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      NIcon {
        icon: "info-circle"
        pointSize: Style.fontSizeXL
        color: Color.mTertiary
        Layout.alignment: Qt.AlignTop
      }

      NText {
        Layout.fillWidth: true
        text: I18n.tr("panels.window-manager.backend-persist-only")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.WordWrap
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Focus section
  // ═══════════════════════════════════════════════════════════════════
  NText {
    text: I18n.tr("panels.window-manager.section-focus")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.focus-policy-label")
    description: I18n.tr("panels.window-manager.focus-policy-description")
    model: root.focusPolicyModel
    currentKey: Settings.data.windowManager.focusPolicy
    defaultValue: Settings.getDefaultValue("windowManager.focusPolicy")
    onSelected: key => Settings.data.windowManager.focusPolicy = key
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: root.ffmActive
    label: I18n.tr("panels.window-manager.ffm-delay-label")
    description: I18n.tr("panels.window-manager.ffm-delay-description")
    minimum: 0
    maximum: 1000
    stepSize: 50
    suffix: " ms"
    value: Settings.data.windowManager.focusFollowsMouseDelay
    onValueChanged: Settings.data.windowManager.focusFollowsMouseDelay = value
    defaultValue: Settings.getDefaultValue("windowManager.focusFollowsMouseDelay")
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.raise-on-click-label")
    description: I18n.tr("panels.window-manager.raise-on-click-description")
    checked: Settings.data.windowManager.raiseOnClick
    onToggled: checked => Settings.data.windowManager.raiseOnClick = checked
    defaultValue: Settings.getDefaultValue("windowManager.raiseOnClick")
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.raise-on-hover-label")
    description: I18n.tr("panels.window-manager.raise-on-hover-description")
    checked: Settings.data.windowManager.raiseOnHover
    onToggled: checked => Settings.data.windowManager.raiseOnHover = checked
    defaultValue: Settings.getDefaultValue("windowManager.raiseOnHover")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Placement section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.window-manager.section-placement")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.placement-label")
    description: I18n.tr("panels.window-manager.placement-description")
    model: root.placementModel
    currentKey: Settings.data.windowManager.placement
    defaultValue: Settings.getDefaultValue("windowManager.placement")
    onSelected: key => Settings.data.windowManager.placement = key
  }

  // ═══════════════════════════════════════════════════════════════════
  // Snapping / edge-tiling section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.window-manager.section-snapping")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.snap-enabled-label")
    description: I18n.tr("panels.window-manager.snap-enabled-description")
    checked: Settings.data.windowManager.snapEnabled
    onToggled: checked => Settings.data.windowManager.snapEnabled = checked
    defaultValue: Settings.getDefaultValue("windowManager.snapEnabled")
  }

  NSpinBox {
    Layout.fillWidth: true
    enabled: Settings.data.windowManager.snapEnabled
    label: I18n.tr("panels.window-manager.snap-distance-label")
    description: I18n.tr("panels.window-manager.snap-distance-description")
    minimum: 1
    maximum: 64
    stepSize: 1
    suffix: " px"
    value: Settings.data.windowManager.snapDistance
    onValueChanged: Settings.data.windowManager.snapDistance = value
    defaultValue: Settings.getDefaultValue("windowManager.snapDistance")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Titlebar & decorations section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.window-manager.section-decorations")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.titlebar-double-click-label")
    description: I18n.tr("panels.window-manager.titlebar-double-click-description")
    model: root.titlebarActionModel
    currentKey: Settings.data.windowManager.titlebarDoubleClick
    defaultValue: Settings.getDefaultValue("windowManager.titlebarDoubleClick")
    onSelected: key => Settings.data.windowManager.titlebarDoubleClick = key
  }

  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.decoration-theme-label")
    description: I18n.tr("panels.window-manager.decoration-theme-description")
    text: Settings.data.windowManager.decorationTheme
    placeholderText: I18n.tr("panels.window-manager.decoration-theme-placeholder")
    defaultValue: Settings.getDefaultValue("windowManager.decorationTheme")
    onEditingFinished: Settings.data.windowManager.decorationTheme = text
  }

  // ═══════════════════════════════════════════════════════════════════
  // WM keyboard shortcuts section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.window-manager.section-shortcuts")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.window-manager.shortcuts-note")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.shortcut-close-label")
    description: I18n.tr("panels.window-manager.shortcut-close-description")
    text: Settings.data.windowManager.shortcutClose
    placeholderText: "Alt+F4"
    defaultValue: Settings.getDefaultValue("windowManager.shortcutClose")
    onEditingFinished: Settings.data.windowManager.shortcutClose = text
  }

  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.shortcut-maximize-label")
    description: I18n.tr("panels.window-manager.shortcut-maximize-description")
    text: Settings.data.windowManager.shortcutToggleMaximize
    placeholderText: "Super+Up"
    defaultValue: Settings.getDefaultValue("windowManager.shortcutToggleMaximize")
    onEditingFinished: Settings.data.windowManager.shortcutToggleMaximize = text
  }

  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.shortcut-fullscreen-label")
    description: I18n.tr("panels.window-manager.shortcut-fullscreen-description")
    text: Settings.data.windowManager.shortcutToggleFullscreen
    placeholderText: "Super+F"
    defaultValue: Settings.getDefaultValue("windowManager.shortcutToggleFullscreen")
    onEditingFinished: Settings.data.windowManager.shortcutToggleFullscreen = text
  }

  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.shortcut-tile-left-label")
    description: I18n.tr("panels.window-manager.shortcut-tile-left-description")
    text: Settings.data.windowManager.shortcutTileLeft
    placeholderText: "Super+Left"
    defaultValue: Settings.getDefaultValue("windowManager.shortcutTileLeft")
    onEditingFinished: Settings.data.windowManager.shortcutTileLeft = text
  }

  NTextInput {
    Layout.fillWidth: true
    label: I18n.tr("panels.window-manager.shortcut-tile-right-label")
    description: I18n.tr("panels.window-manager.shortcut-tile-right-description")
    text: Settings.data.windowManager.shortcutTileRight
    placeholderText: "Super+Right"
    defaultValue: Settings.getDefaultValue("windowManager.shortcutTileRight")
    onEditingFinished: Settings.data.windowManager.shortcutTileRight = text
  }
}
