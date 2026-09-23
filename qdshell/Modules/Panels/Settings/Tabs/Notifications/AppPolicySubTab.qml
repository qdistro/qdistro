import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import qs.Commons
import qs.Services.System
import qs.Widgets

ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // Duplicate suppression toggle
  NToggle {
    label: I18n.tr("panels.notifications.suppress-duplicates-label") || "Suppress duplicate notifications"
    description: I18n.tr("panels.notifications.suppress-duplicates-description") || "Automatically suppress identical notifications received within a short time window."
    checked: Settings.data.notifications.suppressDuplicates !== false
    onToggled: checked => Settings.data.notifications.suppressDuplicates = checked
    defaultValue: Settings.getDefaultValue("notifications.suppressDuplicates")
  }

  NValueSlider {
    Layout.fillWidth: true
    label: I18n.tr("panels.notifications.suppress-duplicates-window-label") || "Duplicate suppression window"
    description: I18n.tr("panels.notifications.suppress-duplicates-window-description") || "Time window (in seconds) during which duplicate notifications are suppressed."
    from: 1
    to: 30
    stepSize: 1
    value: Settings.data.notifications.suppressDuplicateWindowSec || 3
    onMoved: value => Settings.data.notifications.suppressDuplicateWindowSec = value
    text: Math.round(Settings.data.notifications.suppressDuplicateWindowSec || 3) + "s"
    visible: Settings.data.notifications.suppressDuplicates !== false
    defaultValue: Settings.getDefaultValue("notifications.suppressDuplicateWindowSec")
  }

  NDivider {
    Layout.fillWidth: true
  }

  // Per-app policy header
  NText {
    text: I18n.tr("panels.notifications.app-policy-desc") || "Configure notification behavior per application. Apps appear here after sending their first notification."
    wrapMode: Text.WordWrap
    Layout.fillWidth: true
    color: Color.mOnSurfaceVariant
    pointSize: Style.fontSizeS
  }

  // Known apps list
  Repeater {
    id: appRepeater
    model: NotificationService.getKnownApps()

    delegate: NBox {
      Layout.fillWidth: true
      implicitHeight: appColumn.implicitHeight + Style.marginM * 2

      property string appKey: modelData
      property string displayName: NotificationService.getKnownAppDisplayName(appKey)
      property var policy: Settings.data.notifications.appPolicy?.[appKey] || ({})

      ColumnLayout {
        id: appColumn
        anchors.fill: parent
        anchors.margins: Style.marginM
        spacing: Style.marginM

        // App header row
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.marginM

          NText {
            text: displayName
            pointSize: Style.fontSizeM
            font.weight: Style.fontWeightBold
            color: Color.mOnSurface
            Layout.fillWidth: true
          }

          NIconButton {
            icon: "rotate-ccw"
            tooltipText: I18n.tr("panels.notifications.app-policy-reset") || "Reset to defaults"
            baseSize: Style.baseWidgetSize * 0.7
            onClicked: NotificationService.resetAppPolicy(appKey)
          }
        }

        NToggle {
          label: I18n.tr("panels.notifications.app-policy-muted-label") || "Mute notifications"
          description: I18n.tr("panels.notifications.app-policy-muted-description") || "Suppress visual notification popups from this app."
          checked: policy.muted === true
          onToggled: checked => NotificationService.setAppPolicy(appKey, "muted", checked)
        }

        NToggle {
          visible: policy.muted === true
          label: I18n.tr("panels.notifications.app-policy-allow-urgent-label") || "Allow urgent notifications"
          description: I18n.tr("panels.notifications.app-policy-allow-urgent-description") || "Still show critical/urgent notifications even when muted."
          checked: policy.allowUrgent !== false
          onToggled: checked => NotificationService.setAppPolicy(appKey, "allowUrgent", checked)
        }

        NToggle {
          label: I18n.tr("panels.notifications.app-policy-log-label") || "Save to history"
          description: I18n.tr("panels.notifications.app-policy-log-description") || "Record notifications from this app in the notification log."
          checked: policy.logEnabled !== false
          onToggled: checked => NotificationService.setAppPolicy(appKey, "logEnabled", checked)
        }
      }
    }
  }

  // Empty state when no apps have sent notifications yet
  NBox {
    visible: NotificationService.getKnownApps().length === 0
    Layout.fillWidth: true
    Layout.preferredHeight: emptyCol.implicitHeight + Style.marginXL

    ColumnLayout {
      id: emptyCol
      anchors.fill: parent
      anchors.margins: Style.marginM
      spacing: Style.marginM

      Item { Layout.fillHeight: true }

      NIcon {
        icon: "app-window"
        pointSize: 32
        color: Color.mOnSurfaceVariant
        Layout.alignment: Qt.AlignHCenter
      }

      NText {
        text: I18n.tr("panels.notifications.app-policy-empty") || "No apps have sent notifications yet"
        pointSize: Style.fontSizeM
        color: Color.mOnSurfaceVariant
        Layout.alignment: Qt.AlignHCenter
      }

      NText {
        text: I18n.tr("panels.notifications.app-policy-empty-desc") || "Per-app policies will appear here once apps start sending notifications."
        pointSize: Style.fontSizeS
        color: Color.mOnSurfaceVariant
        horizontalAlignment: Text.AlignHCenter
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
      }

      Item { Layout.fillHeight: true }
    }
  }
}
