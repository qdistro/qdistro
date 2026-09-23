import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.Hardware
import qs.Services.UI
import qs.Widgets
import "../../../../../Services/Hardware/PointerInputParse.js" as PointerInputParse

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  Component.onCompleted: {
    PointerInputService.refresh();
  }

  // ─── Helper models ───────────────────────────────────────────────
  readonly property var accelProfileModel: [
    {
      "key": "adaptive",
      "name": I18n.tr("panels.mouse.accel-profile-adaptive")
    },
    {
      "key": "flat",
      "name": I18n.tr("panels.mouse.accel-profile-flat")
    }
  ]

  readonly property var scrollMethodModel: [
    {
      "key": "two_finger",
      "name": I18n.tr("panels.mouse.scroll-method-two-finger")
    },
    {
      "key": "edge",
      "name": I18n.tr("panels.mouse.scroll-method-edge")
    },
    {
      "key": "on_button_down",
      "name": I18n.tr("panels.mouse.scroll-method-button")
    },
    {
      "key": "none",
      "name": I18n.tr("panels.mouse.scroll-method-none")
    }
  ]

  function deviceTypeLabel(type) {
    switch (type) {
    case "touchpad":
      return I18n.tr("panels.mouse.device-type-touchpad");
    case "trackpoint":
      return I18n.tr("panels.mouse.device-type-trackpoint");
    case "mouse":
      return I18n.tr("panels.mouse.device-type-mouse");
    case "tablet":
      return I18n.tr("panels.mouse.device-type-tablet");
    default:
      return I18n.tr("panels.mouse.device-type-pointer");
    }
  }

  function deviceTypeIcon(type) {
    switch (type) {
    case "touchpad":
      return "device-laptop";
    case "trackpoint":
      return "point";
    case "tablet":
      return "edit";
    default:
      return "mouse";
    }
  }

  // Static models for advanced controls.
  readonly property var clickMethodModel: [
    {
      "key": "button_areas",
      "name": I18n.tr("panels.mouse.click-method-button-areas")
    },
    {
      "key": "clickfinger",
      "name": I18n.tr("panels.mouse.click-method-clickfinger")
    }
  ]

  readonly property var tabletAspectModel: [
    {
      "key": "keep",
      "name": I18n.tr("panels.mouse.tablet-aspect-keep")
    },
    {
      "key": "stretch",
      "name": I18n.tr("panels.mouse.tablet-aspect-stretch")
    }
  ]

  // Outputs available for tablet mapping ("" = all outputs first).
  readonly property var tabletOutputModel: {
    var list = [
      {
        "key": "",
        "name": I18n.tr("panels.mouse.tablet-output-all")
      }
    ];
    var screens = Quickshell.screens || [];
    for (var i = 0; i < screens.length; i++) {
      list.push({
        "key": screens[i].name,
        "name": screens[i].name
      });
    }
    return list;
  }

  // Live, normalized tablet mapping (clamped). UI reads from here.
  readonly property var tabletMapping: PointerInputService ? PointerInputParse.normalizeTabletMapping(Settings.data.pointer.tabletMapping) : null
  readonly property bool globalPointerLive: PointerInputService.ready && PointerInputService.canApply

  // Persist a single field of the tablet area, re-normalizing the whole object.
  function updateTabletArea(field, value) {
    var m = PointerInputParse.normalizeTabletMapping(Settings.data.pointer.tabletMapping);
    m.area[field] = value / 100.0;
    PointerInputService.setTabletMapping(m);
  }

  // ═══════════════════════════════════════════════════════════════════
  // Backend status banners. qdwin v28 can apply the global libinput snapshot;
  // advanced controls below still say persist-only at their own sections.
  // ═══════════════════════════════════════════════════════════════════
  Rectangle {
    Layout.fillWidth: true
    visible: PointerInputService.ready && !PointerInputService.canApply
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
        text: I18n.tr("panels.mouse.backend-persist-only")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.WordWrap
      }
    }
  }

  Rectangle {
    Layout.fillWidth: true
    visible: root.globalPointerLive
    radius: Style.iRadiusS
    color: Color.mSurfaceVariant
    border.color: Color.mOutline
    border.width: Style.borderS
    implicitHeight: liveRow.implicitHeight + Style.marginM * 2

    RowLayout {
      id: liveRow
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
        text: I18n.tr("panels.mouse.live-fields-note")
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeS
        wrapMode: Text.WordWrap
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Devices section
  // ═══════════════════════════════════════════════════════════════════
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM

    NText {
      text: I18n.tr("panels.mouse.section-devices")
      pointSize: Style.fontSizeM
      font.weight: Style.fontWeightBold
      color: Color.mPrimary
      Layout.fillWidth: true
    }

    NButton {
      text: I18n.tr("common.refresh")
      icon: "filepicker-refresh"
      outlined: true
      onClicked: PointerInputService.refresh()
    }
  }

  // Empty / unavailable state
  NLabel {
    Layout.fillWidth: true
    visible: PointerInputService.ready && !PointerInputService.hasDevices
    label: I18n.tr("panels.mouse.no-devices-label")
    description: I18n.tr("panels.mouse.no-devices-description")
  }

  // Device rows
  Repeater {
    model: PointerInputService.devices

    delegate: Rectangle {
      Layout.fillWidth: true
      implicitHeight: deviceRow.implicitHeight + Style.marginM * 2
      radius: Style.iRadiusS
      color: "transparent"
      border.color: Color.mOutline
      border.width: Style.borderS

      RowLayout {
        id: deviceRow
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        NIcon {
          icon: root.deviceTypeIcon(modelData.type)
          pointSize: Style.fontSizeXXL
          color: Color.mPrimary
          Layout.alignment: Qt.AlignVCenter
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.marginXXS

          NText {
            text: modelData.name
            pointSize: Style.fontSizeM
            font.weight: Style.fontWeightSemiBold
            color: Color.mOnSurface
            Layout.fillWidth: true
            elide: Text.ElideRight
            maximumLineCount: 1
          }

          NText {
            text: root.deviceTypeLabel(modelData.type)
            pointSize: Style.fontSizeS
            color: Color.mOnSurfaceVariant
            Layout.fillWidth: true
          }
        }
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Pointer behaviour section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-pointer")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.accel-profile-label")
    description: I18n.tr("panels.mouse.accel-profile-description")
    model: root.accelProfileModel
    currentKey: Settings.data.pointer.accelProfile
    defaultValue: Settings.getDefaultValue("pointer.accelProfile")
    onSelected: key => Settings.data.pointer.accelProfile = key
  }

  NValueSlider {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.speed-label")
    description: I18n.tr("panels.mouse.speed-description")
    from: 0.0
    to: 1.0
    stepSize: 0.05
    value: Settings.data.pointer.pointerSpeed
    text: Math.round(Settings.data.pointer.pointerSpeed * 100) + "%"
    defaultValue: Settings.getDefaultValue("pointer.pointerSpeed")
    onMoved: value => Settings.data.pointer.pointerSpeed = value
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.left-handed-label")
    description: I18n.tr("panels.mouse.left-handed-description")
    checked: Settings.data.pointer.leftHanded
    onToggled: checked => Settings.data.pointer.leftHanded = checked
    defaultValue: Settings.getDefaultValue("pointer.leftHanded")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Buttons & Click section (libinput click method, middle-click emulation)
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-buttons")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.click-method-label")
    description: I18n.tr("panels.mouse.click-method-description")
    model: root.clickMethodModel
    currentKey: Settings.data.pointer.clickMethod
    defaultValue: Settings.getDefaultValue("pointer.clickMethod")
    onSelected: key => Settings.data.pointer.clickMethod = key
  }

  NText {
    Layout.fillWidth: true
    visible: root.globalPointerLive
    text: I18n.tr("panels.mouse.click-method-note")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.middle-click-emulation-label")
    description: I18n.tr("panels.mouse.middle-click-emulation-description")
    checked: Settings.data.pointer.middleClickEmulation
    onToggled: checked => Settings.data.pointer.middleClickEmulation = checked
    defaultValue: Settings.getDefaultValue("pointer.middleClickEmulation")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Scrolling section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-scrolling")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.natural-scroll-label")
    description: I18n.tr("panels.mouse.natural-scroll-description")
    checked: Settings.data.pointer.naturalScroll
    onToggled: checked => Settings.data.pointer.naturalScroll = checked
    defaultValue: Settings.getDefaultValue("pointer.naturalScroll")
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.horizontal-scroll-label")
    description: I18n.tr("panels.mouse.horizontal-scroll-description")
    checked: Settings.data.pointer.horizontalScroll
    onToggled: checked => Settings.data.pointer.horizontalScroll = checked
    defaultValue: Settings.getDefaultValue("pointer.horizontalScroll")
  }

  NText {
    Layout.fillWidth: true
    visible: root.globalPointerLive
    text: I18n.tr("panels.mouse.horizontal-scroll-note")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  NComboBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.scroll-method-label")
    description: I18n.tr("panels.mouse.scroll-method-description")
    model: root.scrollMethodModel
    currentKey: Settings.data.pointer.scrollMethod
    defaultValue: Settings.getDefaultValue("pointer.scrollMethod")
    onSelected: key => Settings.data.pointer.scrollMethod = key
  }

  // ═══════════════════════════════════════════════════════════════════
  // Touchpad section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-touchpad")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.tap-to-click-label")
    description: I18n.tr("panels.mouse.tap-to-click-description")
    checked: Settings.data.pointer.tapToClick
    onToggled: checked => Settings.data.pointer.tapToClick = checked
    defaultValue: Settings.getDefaultValue("pointer.tapToClick")
  }

  NToggle {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.disable-while-typing-label")
    description: I18n.tr("panels.mouse.disable-while-typing-description")
    checked: Settings.data.pointer.disableWhileTyping
    onToggled: checked => Settings.data.pointer.disableWhileTyping = checked
    defaultValue: Settings.getDefaultValue("pointer.disableWhileTyping")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Double-click & drag section (compositor-independent UI behaviour)
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-doubleclick")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.mouse.doubleclick-note")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  NSpinBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.double-click-time-label")
    description: I18n.tr("panels.mouse.double-click-time-description")
    minimum: 100
    maximum: 1000
    stepSize: 50
    suffix: " ms"
    value: Settings.data.pointer.doubleClickTime
    onValueChanged: Settings.data.pointer.doubleClickTime = value
    defaultValue: Settings.getDefaultValue("pointer.doubleClickTime")
  }

  NSpinBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.double-click-distance-label")
    description: I18n.tr("panels.mouse.double-click-distance-description")
    minimum: 1
    maximum: 30
    stepSize: 1
    suffix: " px"
    value: Settings.data.pointer.doubleClickDistance
    onValueChanged: Settings.data.pointer.doubleClickDistance = value
    defaultValue: Settings.getDefaultValue("pointer.doubleClickDistance")
  }

  NSpinBox {
    Layout.fillWidth: true
    label: I18n.tr("panels.mouse.drag-threshold-label")
    description: I18n.tr("panels.mouse.drag-threshold-description")
    minimum: 1
    maximum: 50
    stepSize: 1
    suffix: " px"
    value: Settings.data.pointer.dragThreshold
    onValueChanged: Settings.data.pointer.dragThreshold = value
    defaultValue: Settings.getDefaultValue("pointer.dragThreshold")
  }

  // ═══════════════════════════════════════════════════════════════════
  // Per-device overrides section
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-per-device")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.mouse.per-device-description")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  NLabel {
    Layout.fillWidth: true
    visible: PointerInputService.ready && !PointerInputService.hasDevices
    label: I18n.tr("panels.mouse.no-devices-label")
  }

  Repeater {
    model: PointerInputService.devices

    delegate: Rectangle {
      // Per-device override card. Touchpad/mouse controls are exposed; tablets
      // expose only enable/disable here (their mapping lives in its own section).
      readonly property string devId: modelData.id
      readonly property bool isTablet: modelData.type === "tablet"

      Layout.fillWidth: true
      implicitHeight: perDeviceCol.implicitHeight + Style.marginM * 2
      radius: Style.iRadiusS
      color: "transparent"
      border.color: Color.mOutline
      border.width: Style.borderS

      ColumnLayout {
        id: perDeviceCol
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginS

        RowLayout {
          Layout.fillWidth: true
          spacing: Style.marginM

          NIcon {
            icon: root.deviceTypeIcon(modelData.type)
            pointSize: Style.fontSizeXL
            color: Color.mPrimary
            Layout.alignment: Qt.AlignVCenter
          }

          ColumnLayout {
            Layout.fillWidth: true
            spacing: Style.marginXXS

            NText {
              // Untrusted device name — rendered as PlainText (NText default).
              text: modelData.name
              pointSize: Style.fontSizeM
              font.weight: Style.fontWeightSemiBold
              color: Color.mOnSurface
              Layout.fillWidth: true
              elide: Text.ElideRight
              maximumLineCount: 1
            }

            NText {
              text: root.deviceTypeLabel(modelData.type)
              pointSize: Style.fontSizeS
              color: Color.mOnSurfaceVariant
              Layout.fillWidth: true
            }
          }
        }

        NToggle {
          Layout.fillWidth: true
          label: I18n.tr("panels.mouse.device-enabled-label")
          description: I18n.tr("panels.mouse.device-enabled-description")
          // Bind to disabledDevices so the toggle reflects external changes.
          checked: !PointerInputParse.isDeviceDisabled(Settings.data.pointer.disabledDevices, devId)
          onToggled: checked => PointerInputService.setDeviceEnabled(devId, checked)
        }

        NToggle {
          Layout.fillWidth: true
          visible: !isTablet
          label: I18n.tr("panels.mouse.device-customize-label")
          description: I18n.tr("panels.mouse.device-customize-description")
          checked: PointerInputParse.hasDeviceOverride(Settings.data.pointer.perDeviceOverrides, devId)
          onToggled: checked => {
            if (checked) {
              // Seed the override with the current global speed so the override
              // map is non-empty (which is what "has override" keys on).
              PointerInputService.setOverride(devId, "pointerSpeed", Settings.data.pointer.pointerSpeed);
            } else {
              PointerInputService.clearOverride(devId);
            }
          }
        }

        ColumnLayout {
          Layout.fillWidth: true
          spacing: Style.marginS
          visible: !isTablet && PointerInputParse.hasDeviceOverride(Settings.data.pointer.perDeviceOverrides, devId)

          NComboBox {
            Layout.fillWidth: true
            label: I18n.tr("panels.mouse.accel-profile-label")
            model: root.accelProfileModel
            currentKey: PointerInputService.effectiveSettings(devId).accelProfile
            onSelected: key => PointerInputService.setOverride(devId, "accelProfile", key)
          }

          NValueSlider {
            Layout.fillWidth: true
            label: I18n.tr("panels.mouse.speed-label")
            from: 0.0
            to: 1.0
            stepSize: 0.05
            value: PointerInputService.effectiveSettings(devId).pointerSpeed
            text: Math.round(PointerInputService.effectiveSettings(devId).pointerSpeed * 100) + "%"
            onMoved: value => PointerInputService.setOverride(devId, "pointerSpeed", value)
          }

          NToggle {
            Layout.fillWidth: true
            label: I18n.tr("panels.mouse.natural-scroll-label")
            checked: PointerInputService.effectiveSettings(devId).naturalScroll
            onToggled: checked => PointerInputService.setOverride(devId, "naturalScroll", checked)
          }

          NToggle {
            Layout.fillWidth: true
            label: I18n.tr("panels.mouse.left-handed-label")
            checked: PointerInputService.effectiveSettings(devId).leftHanded
            onToggled: checked => PointerInputService.setOverride(devId, "leftHanded", checked)
          }

          NButton {
            text: I18n.tr("panels.mouse.device-reset-override")
            icon: "filepicker-refresh"
            outlined: true
            onClicked: PointerInputService.clearOverride(devId)
          }
        }
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Tablet mapping section (Wacom / graphics tablets; persist-only)
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    text: I18n.tr("panels.mouse.section-tablet")
    pointSize: Style.fontSizeM
    font.weight: Style.fontWeightBold
    color: Color.mPrimary
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.mouse.tablet-description")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeXS
    wrapMode: Text.WordWrap
  }

  NLabel {
    Layout.fillWidth: true
    visible: PointerInputService.ready && !PointerInputService.hasTablet
    label: I18n.tr("panels.mouse.no-tablet-label")
    description: I18n.tr("panels.mouse.no-tablet-description")
  }

  ColumnLayout {
    Layout.fillWidth: true
    spacing: Style.marginM
    visible: PointerInputService.hasTablet

    NComboBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.mouse.tablet-output-label")
      description: I18n.tr("panels.mouse.tablet-output-description")
      model: root.tabletOutputModel
      currentKey: root.tabletMapping ? root.tabletMapping.output : ""
      onSelected: key => {
        var m = PointerInputParse.normalizeTabletMapping(Settings.data.pointer.tabletMapping);
        m.output = key;
        PointerInputService.setTabletMapping(m);
      }
    }

    NComboBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.mouse.tablet-aspect-label")
      description: I18n.tr("panels.mouse.tablet-aspect-description")
      model: root.tabletAspectModel
      currentKey: root.tabletMapping ? root.tabletMapping.aspect : "keep"
      onSelected: key => {
        var m = PointerInputParse.normalizeTabletMapping(Settings.data.pointer.tabletMapping);
        m.aspect = key;
        PointerInputService.setTabletMapping(m);
      }
    }

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.mouse.tablet-area-x-label")
      description: I18n.tr("panels.mouse.tablet-area-x-description")
      minimum: 0
      maximum: 100
      stepSize: 5
      suffix: " %"
      value: root.tabletMapping ? Math.round(root.tabletMapping.area.x * 100) : 0
      onValueChanged: root.updateTabletArea("x", value)
    }

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.mouse.tablet-area-y-label")
      description: I18n.tr("panels.mouse.tablet-area-y-description")
      minimum: 0
      maximum: 100
      stepSize: 5
      suffix: " %"
      value: root.tabletMapping ? Math.round(root.tabletMapping.area.y * 100) : 0
      onValueChanged: root.updateTabletArea("y", value)
    }

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.mouse.tablet-area-w-label")
      description: I18n.tr("panels.mouse.tablet-area-w-description")
      minimum: 1
      maximum: 100
      stepSize: 5
      suffix: " %"
      value: root.tabletMapping ? Math.round(root.tabletMapping.area.w * 100) : 100
      onValueChanged: root.updateTabletArea("w", value)
    }

    NSpinBox {
      Layout.fillWidth: true
      label: I18n.tr("panels.mouse.tablet-area-h-label")
      description: I18n.tr("panels.mouse.tablet-area-h-description")
      minimum: 1
      maximum: 100
      stepSize: 5
      suffix: " %"
      value: root.tabletMapping ? Math.round(root.tabletMapping.area.h * 100) : 100
      onValueChanged: root.updateTabletArea("h", value)
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // Cursor note (cursor theme/size lives in the Appearance tab)
  // ═══════════════════════════════════════════════════════════════════
  NDivider {
    Layout.fillWidth: true
    Layout.topMargin: Style.marginM
    Layout.bottomMargin: Style.marginM
  }

  NText {
    Layout.fillWidth: true
    text: I18n.tr("panels.mouse.cursor-hint")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
    wrapMode: Text.WordWrap
  }
}
