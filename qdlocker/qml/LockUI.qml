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

  Rectangle { anchors.fill: parent; color: Color.mSurface }

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
