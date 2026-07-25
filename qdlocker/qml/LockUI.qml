// LockUI — pure-QtQuick lock screen, no Quickshell dependency.
//
// Uses the local styling shim (shim/Color.qml, shim/Style.qml) which
// mirrors qdshell's Commons palette and metrics with hardcoded values.
// No Quickshell, Settings, or Service imports required.
//
// Property naming: `lockController` (not `controller`) so context-
// property shadowing in Main.qml doesn't surprise the reader. The
// controller exposes (from qdlocker/controller.py):
//   currentText, infoMessage, errorMessage,
//   showInfo, showFailure, pamReady, unlockInProgress
// with NOTIFY signals; QML two-way binding via TextInput works
// because the Python setter is idempotent on equal values.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import shim

Item {
  id: root
  property var lockController

  // Live-capture / network-egress indicators (contextProperty "indicators",
  // see app.py and qdlocker/indicators.py). If the context property is missing
  // the banner says so rather than rendering nothing: a lock surface that
  // cannot observe capture must not look like a quiet machine.
  property var lockIndicators: (typeof indicators !== "undefined") ? indicators : null

  Rectangle { anchors.fill: parent; color: Color.mSurface }

  // Security indicator banner — sessions.md requires non-suppressible state
  // for live mic/camera/screencast/system-audio/virtual-input capture and
  // qdistro network egress. Nothing gates this on a setting.
  //
  // Three distinct severities, because they mean different things:
  //   * observed capture (red) — a running PipeWire capture node, named by
  //     its own client where the graph attributes one, otherwise reported as
  //     device-level activity with the client unknown;
  //   * observer failed (red) — no reading at all, which is NOT the same as
  //     a quiet machine and must not look like one;
  //   * coverage disclosure (dim) — the standing statement of what this
  //     cannot see: direct /dev/snd + /dev/video grants, weston_capture_v1
  //     grabs and virtual input are unmonitored, so "nothing observed" is
  //     never "nothing is happening". See qdlocker/indicators.py.
  Rectangle {
    id: securityBanner
    anchors.top: parent.top
    anchors.horizontalCenter: parent.horizontalCenter
    anchors.topMargin: Style.fontSizeXXL
    width: Math.min(parent.width * 0.9, 900)
    height: bannerRows.implicitHeight + Style.fontSizeXXL
    radius: Style.radiusXS
    readonly property bool capturing: root.lockIndicators
                                      ? root.lockIndicators.captureActive : false
    // No reading at all: either the observer object is missing or every kind
    // is unverified with nothing observed.
    readonly property bool observerDead: !root.lockIndicators
    color: (capturing || observerDead) ? Qt.alpha(Color.mError, 0.18)
                                       : Qt.alpha(Color.mOnSurfaceVariant, 0.10)
    border.color: (capturing || observerDead) ? Color.mError
                                              : Qt.alpha(Color.mOnSurfaceVariant, 0.35)
    border.width: Style.borderM

    Column {
      id: bannerRows
      anchors.centerIn: parent
      width: parent.width - Style.fontSizeXXL
      spacing: Style.fontSizeS

      // Observer missing entirely (no context property) — say so loudly.
      Text {
        width: parent.width
        visible: securityBanner.observerDead
        text: "⚠ capture monitoring unavailable — mic, camera and screen capture are NOT being observed"
        wrapMode: Text.WordWrap
        color: Color.mError
        font.pointSize: Style.fontSizeL
      }

      // Positively observed capture. "LIVE CAPTURE" only when the graph
      // attributes a client; device-level evidence says so instead.
      Text {
        width: parent.width
        visible: securityBanner.capturing
        text: (root.lockIndicators && root.lockIndicators.captureAttributed
               ? "⚠ LIVE CAPTURE: " : "⚠ CAPTURE ACTIVITY: ")
              + (root.lockIndicators ? root.lockIndicators.captureDetail : "")
        wrapMode: Text.WordWrap
        color: Color.mError
        font.pointSize: Style.fontSizeL
      }

      // Standing coverage disclosure. Deliberatelylow-key relative to the rows
      // above: it is always true, so it must not compete with a real event.
      Text {
        width: parent.width
        visible: root.lockIndicators ? root.lockIndicators.captureUnverified : false
        text: "capture monitoring: partial — no capture observed for "
              + (root.lockIndicators ? root.lockIndicators.captureUnverifiedLabel : "")
              + "; direct device grants and virtual input are not monitored"
        wrapMode: Text.WordWrap
        color: Color.mOnSurfaceVariant
        font.pointSize: Style.fontSizeM
      }

      // Active silo network egress, and its own unverified case.
      Text {
        width: parent.width
        visible: root.lockIndicators ? root.lockIndicators.egressActive : false
        text: "network egress: " + (root.lockIndicators
                                    ? root.lockIndicators.egressLabel : "")
        wrapMode: Text.WordWrap
        color: Color.mPrimary
        font.pointSize: Style.fontSizeM
      }
      Text {
        width: parent.width
        visible: root.lockIndicators ? root.lockIndicators.egressUnverified : false
        text: "⚠ network egress state unverified (session manager unreachable)"
        wrapMode: Text.WordWrap
        color: Color.mError
        font.pointSize: Style.fontSizeM
      }
    }
  }

  // Clock — updated by a Timer (`new Date()` in a binding is not
  // reactive).
  property string clockText: ""
  property string dateText: ""
  Timer {
    interval: 1000; repeat: true; running: true; triggeredOnStart: true
    onTriggered: {
      const now = new Date()
      root.clockText = Qt.formatDateTime(now, "HH:mm")
      root.dateText = Qt.formatDateTime(now, "dddd, MMMM d")
    }
  }

  ColumnLayout {
    anchors.centerIn: parent
    spacing: Style.fontSizeXXXL
    width: Math.min(parent.width * 0.5, 480)

    Text {
      Layout.alignment: Qt.AlignHCenter
      text: root.clockText
      font.pointSize: 56
      color: Color.mOnSurface
    }
    Text {
      Layout.alignment: Qt.AlignHCenter
      text: root.dateText
      font.pointSize: Style.fontSizeXXL
      color: Color.mOnSurfaceVariant
    }

    // Combined info/failure banner — one Rectangle, switch role on
    // showFailure. Mutually-exclusive bubbles previously double-
    // rendered during the transition window.
    Rectangle {
      Layout.fillWidth: true
      height: 44
      radius: Style.radiusXS
      visible: lockController
               && (lockController.showInfo || lockController.showFailure)
               && (lockController.infoMessage.length > 0
                   || lockController.errorMessage.length > 0)
      color: lockController && lockController.showFailure
             ? Qt.alpha(Color.mError, 0.15)
             : Qt.alpha(Color.mPrimary, 0.10)

      Text {
        anchors.centerIn: parent
        text: lockController
              ? (lockController.showFailure
                 ? lockController.errorMessage
                 : lockController.infoMessage)
              : ""
        color: lockController && lockController.showFailure
               ? Color.mError
               : Color.mPrimary
        font.pointSize: Style.fontSizeL
      }
    }

    // Password "field" — purely a visual rendering of the buffer
    // held in `lockController.currentText`. Input arrives via the
    // compositor's overlay_key channel (see controller.py
    // `handle_overlay_key`); no Qt TextInput is involved, so the
    // QML doesn't compete for keyboard focus with the controller.
    //
    // Earlier drafts placed a `width:0; height:0; visible:false`
    // TextInput here with `Keys.onPressed` to catch Enter. That
    // didn't work: `visible:false` Items are not focus-eligible in
    // Qt Quick, and `forceActiveFocus()` silently no-ops. The
    // Enter-to-submit path now lives in controller.handle_overlay_key
    // matching on XKB_Return.
    Rectangle {
      id: passwordField
      Layout.fillWidth: true
      height: 48
      radius: Style.radiusXS
      color: Color.mSurfaceVariant
      border.color: Color.mPrimary
      border.width: Style.borderM
      opacity: lockController && lockController.unlockInProgress ? 0.6 : 1.0
      Text {
        anchors.centerIn: parent
        text: lockController
              ? "•".repeat(lockController.currentText.length)
              : ""
        font.pointSize: 22
        color: Color.mOnSurface
      }
    }

    BusyIndicator {
      Layout.alignment: Qt.AlignHCenter
      visible: lockController && lockController.unlockInProgress
      running: visible
    }
  }
}
