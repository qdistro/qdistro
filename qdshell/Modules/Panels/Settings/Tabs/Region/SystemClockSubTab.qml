import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Widgets

// System Clock sub-tab — system date/time/timezone/NTP control via
// systemd-timedated (SystemClockService). Owns OS state (not a qdshell
// setting): everything reflects the live `timedatectl show` state and writes
// back with `timedatectl set-*`.
ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // ListModel for the timezone picker, rebuilt from the enumerated set.
  ListModel {
    id: timezoneModel
  }

  function rebuildTimezoneModel() {
    timezoneModel.clear();
    var zs = SystemClockService.timezones;
    for (var i = 0; i < zs.length; i++)
      timezoneModel.append({
                             "key": zs[i],
                             "name": zs[i]
                           });
  }

  Component.onCompleted: {
    SystemClockService.init();
    rebuildTimezoneModel();
  }

  Connections {
    target: SystemClockService
    function onTimezonesLoadedChanged() {
      root.rebuildTimezoneModel();
    }
  }

  // ─── Unavailable note (no timedatectl) ──────────────────────────────
  NText {
    Layout.fillWidth: true
    visible: SystemClockService.capabilitiesReady && !SystemClockService.hasTimedatectl
    text: I18n.tr("panels.location.system-clock-unavailable")
    color: Color.mOnSurfaceVariant
    wrapMode: Text.WordWrap
  }

  // ─── Error / permission-denied banner ───────────────────────────────
  // Surfaces a clear state when timedatectl rejected a change. Never hidden on
  // failure: the service re-reads real state so the controls revert.
  Rectangle {
    Layout.fillWidth: true
    visible: SystemClockService.lastError !== ""
    radius: Style.iRadiusM
    color: Color.mError
    Layout.preferredHeight: errorRow.implicitHeight + Style.marginM * 2

    RowLayout {
      id: errorRow
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      NIcon {
        icon: "warning"
        color: Color.mOnError
      }
      NText {
        Layout.fillWidth: true
        color: Color.mOnError
        wrapMode: Text.WordWrap
        text: {
          if (SystemClockService.lastError === "denied")
            return I18n.tr("panels.location.system-clock-error-denied");
          if (SystemClockService.lastError === "rejected")
            return I18n.tr("panels.location.system-clock-error-invalid");
          return I18n.tr("panels.location.system-clock-error-generic");
        }
      }
      NIconButton {
        icon: "close"
        onClicked: SystemClockService.clearError()
      }
    }
  }

  // ─── Timezone picker ────────────────────────────────────────────────
  NSearchableComboBox {
    Layout.fillWidth: true
    enabled: SystemClockService.available && SystemClockService.timezonesLoaded && !SystemClockService.applying
    label: I18n.tr("panels.location.system-clock-timezone-label")
    description: I18n.tr("panels.location.system-clock-timezone-description")
    model: timezoneModel
    currentKey: SystemClockService.timezone
    placeholder: I18n.tr("panels.location.system-clock-timezone-placeholder")
    searchPlaceholder: I18n.tr("panels.location.system-clock-timezone-search-placeholder")
    popupHeight: 360
    // SECURITY: setTimezone validates the key against the enumerated zone list
    // in the pure module before any command is built.
    onSelected: key => SystemClockService.setTimezone(key)
  }

  // ─── NTP / automatic time sync toggle ───────────────────────────────
  NToggle {
    enabled: SystemClockService.available && SystemClockService.stateReady && SystemClockService.canNTP && !SystemClockService.applying
    label: I18n.tr("panels.location.system-clock-ntp-label")
    description: I18n.tr("panels.location.system-clock-ntp-description")
    checked: SystemClockService.ntp
    onToggled: checked => SystemClockService.setNtp(checked)
  }

  // ─── NTP sync status (informational) ────────────────────────────────
  NText {
    Layout.fillWidth: true
    visible: SystemClockService.available && SystemClockService.ntp
    text: SystemClockService.ntpSynchronized ? I18n.tr("panels.location.system-clock-ntp-synced") : I18n.tr("panels.location.system-clock-ntp-not-synced")
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
  }

  // ─── Manual date/time ───────────────────────────────────────────────
  // Disabled/read-only when NTP is ON (timedatectl refuses set-time then).
  NTextInputButton {
    Layout.fillWidth: true
    enabled: SystemClockService.available && SystemClockService.stateReady && !SystemClockService.ntp && !SystemClockService.applying
    label: I18n.tr("panels.location.system-clock-manual-label")
    description: SystemClockService.ntp ? I18n.tr("panels.location.system-clock-manual-disabled-description") : I18n.tr("panels.location.system-clock-manual-description")
    placeholderText: "YYYY-MM-DD HH:MM:SS"
    buttonIcon: "check"
    buttonTooltip: I18n.tr("panels.location.system-clock-manual-apply")
    buttonEnabled: enabled
    // SECURITY: setTime validates the string (strict regex + range check) in
    // the pure module before any command is built.
    onButtonClicked: SystemClockService.setTime(text)
  }
}
