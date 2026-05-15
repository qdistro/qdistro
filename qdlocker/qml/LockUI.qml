// LockUI — pure-QtQuick lock screen, no Quickshell dependency.
//
// The qdshell widgets (NText, NIcon, NIconButton, NBusyIndicator)
// transitively require Quickshell.* imports and Settings/Service
// singletons that don't exist outside a Quickshell process. Until
// those land in a Quickshell-free shim, qdlocker renders its own
// minimal chrome with the same colour intent and font scale.
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

Item {
  id: root
  property var lockController

  Rectangle { anchors.fill: parent; color: "#101015" }

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
    spacing: 24
    width: Math.min(parent.width * 0.5, 480)

    Text {
      Layout.alignment: Qt.AlignHCenter
      text: root.clockText
      font.pointSize: 56
      color: "white"
    }
    Text {
      Layout.alignment: Qt.AlignHCenter
      text: root.dateText
      font.pointSize: 18
      color: "#a8a8b0"
    }

    // Combined info/failure banner — one Rectangle, switch role on
    // showFailure. Mutually-exclusive bubbles previously double-
    // rendered during the transition window.
    Rectangle {
      Layout.fillWidth: true
      height: 44
      radius: 6
      visible: lockController
               && (lockController.showInfo || lockController.showFailure)
               && (lockController.infoMessage.length > 0
                   || lockController.errorMessage.length > 0)
      color: lockController && lockController.showFailure ? "#3a1a1a" : "#1a2a3a"

      Text {
        anchors.centerIn: parent
        text: lockController
              ? (lockController.showFailure
                 ? lockController.errorMessage
                 : lockController.infoMessage)
              : ""
        color: lockController && lockController.showFailure ? "#ff8080" : "#80c0ff"
        font.pointSize: 14
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
      radius: 6
      color: "#202028"
      border.color: "#80c0ff"
      border.width: 2
      opacity: lockController && lockController.unlockInProgress ? 0.6 : 1.0
      Text {
        anchors.centerIn: parent
        text: lockController
              ? "•".repeat(lockController.currentText.length)
              : ""
        font.pointSize: 22
        color: "white"
      }
    }

    BusyIndicator {
      Layout.alignment: Qt.AlignHCenter
      visible: lockController && lockController.unlockInProgress
      running: visible
    }
  }
}
