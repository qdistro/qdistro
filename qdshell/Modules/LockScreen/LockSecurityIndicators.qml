import QtQuick
import QtQuick.Layouts
import qs.Commons
import qs.Services.Qdistro
import qs.Widgets

// Minimal non-suppressible security strip: live capture + active silo network
// egress. Used on locked outputs whose decorative content is switched off
// (Settings.data.general.lockScreenMonitors excludes them), so blanking an
// output cannot also blank the indicators the owner is entitled to see.
//
// The full-fat versions of these rows live in LockScreenPanel.qml; this is the
// same state, rendered small.
Item {
  id: root

  implicitWidth: strip.implicitWidth + Style.marginL * 2
  implicitHeight: strip.implicitHeight + Style.marginM * 2

  Rectangle {
    anchors.fill: parent
    radius: Style.radiusL
    color: Color.mSurface
    opacity: 0.85
  }

  RowLayout {
    id: strip
    anchors.centerIn: parent
    spacing: Style.marginL

    RowLayout {
      spacing: 6
      visible: SiloEgressService.active

      NIcon {
        icon: "network"
        pointSize: Style.fontSizeM
        color: Color.mPrimary
      }

      NText {
        text: SiloEgressService.activeCount + " net"
        color: Color.mOnSurfaceVariant
        pointSize: Style.fontSizeM
      }
    }

    RowLayout {
      spacing: 6
      visible: CaptureStateService.indicatorVisible

      NIcon {
        icon: CaptureStateService.anyActive ? "alert-triangle" : "question-mark"
        pointSize: Style.fontSizeM
        color: CaptureStateService.anyActive ? Color.mError : Color.mOnSurfaceVariant
      }

      NText {
        text: (CaptureStateService.anyActive ? CaptureStateService.activeDetail : "capture") + (CaptureStateService.anyUnverified ? " ?" : "")
        color: CaptureStateService.anyActive ? Color.mError : Color.mOnSurfaceVariant
        pointSize: Style.fontSizeM
        elide: Text.ElideRight
        Layout.maximumWidth: 320
      }
    }
  }
}
