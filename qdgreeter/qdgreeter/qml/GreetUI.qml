// GreetUI — pure-QtQuick boot greeter, no Quickshell dependency.
//
// Minimal sign-in chrome: username field (auto-filled, read-only —
// single-user qdistro), password field, submit button, error label.
//
// Per plan2/tasks/P01: deliberately minimal — branding polish is
// reserved for a follow-up task.  Palette and metrics come from the
// shim module (qdshell default dark theme) rather than inline hex
// literals, so the greeter tracks qdshell's styling in one place.
//
// Controller interface (from qdgreeter/controller.py):
//   username, currentText, statusMessage, busy
// with NOTIFY signals; submit() slot.

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import shim

Item {
  id: root
  property var controller

  function forcePasswordFocus() {
    passwordInput.forceActiveFocus()
  }

  function handleTtySwitch(event) {
    if (!controller)
      return false
    if (!(event.modifiers & Qt.ControlModifier) || !(event.modifiers & Qt.AltModifier))
      return false

    const first = Qt.Key_F1
    const last = Qt.Key_F6
    if (event.key < first || event.key > last)
      return false

    controller.switchToTty(event.key - first + 1)
    event.accepted = true
    return true
  }

  Rectangle {
    anchors.fill: parent
    color: Color.mSurface
  }

  ColumnLayout {
    anchors.centerIn: parent
    spacing: Style.marginXL
    width: Math.min(parent.width * 0.5, 520)

    Text {
      text: "👤"
      font.pointSize: Style.fontSizeXXXL * 2
      color: Color.mPrimary
      Layout.alignment: Qt.AlignHCenter
    }

    Text {
      text: "Welcome to qdistro"
      font.pointSize: Style.fontSizeXXXL
      color: Color.mOnSurface
      Layout.alignment: Qt.AlignHCenter
    }

    // Username field. Auto-filled from controller.username; read-only
    // because qdistro is single-user (admin). Showing it explicitly
    // (vs. just a label) keeps the UI honest about *who* the password
    // is unlocking — the greeter is the only place a future user
    // picker could plausibly live.
    Rectangle {
      Layout.fillWidth: true
      height: 48
      radius: Style.radiusXS
      color: Color.mSurfaceVariant
      border.width: Style.borderS
      border.color: Color.mOutline

      TextInput {
        id: usernameInput
        objectName: "qdgreeter.username"
        anchors.fill: parent
        anchors.leftMargin: Style.marginL
        anchors.rightMargin: Style.marginL
        verticalAlignment: TextInput.AlignVCenter
        font.pointSize: Style.fontSizeXL
        color: Color.mOnSurface
        readOnly: true
        text: controller ? controller.username : "admin"
      }
    }

    Rectangle {
      Layout.fillWidth: true
      height: 48
      radius: Style.radiusXS
      color: Color.mSurfaceVariant
      border.width: passwordInput.activeFocus ? Style.borderM : Style.borderS
      border.color: passwordInput.activeFocus ? Color.mPrimary : Color.mOutline

      TextInput {
        id: passwordInput
        objectName: "qdgreeter.password"
        anchors.fill: parent
        anchors.leftMargin: Style.marginL
        anchors.rightMargin: Style.marginL
        verticalAlignment: TextInput.AlignVCenter
        font.pointSize: Style.fontSizeXL
        color: Color.mOnSurface
        echoMode: TextInput.Password
        passwordCharacter: "•"
        enabled: controller ? !controller.busy : false
        focus: true
        activeFocusOnTab: true
        text: controller ? controller.currentText : ""
        onTextChanged: if (controller) controller.currentText = text
        Connections {
          target: controller
          function onCurrentTextChanged() {
            if (passwordInput.text !== controller.currentText)
              passwordInput.text = controller.currentText
          }
        }
        Keys.onPressed: function (event) {
          if (root.handleTtySwitch(event))
            return
          if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (controller) controller.submit()
            event.accepted = true
          }
        }
        Component.onCompleted: root.forcePasswordFocus()
      }
    }

    Timer {
      interval: 250
      repeat: true
      running: true
      onTriggered: if (!passwordInput.activeFocus) passwordInput.forceActiveFocus()
    }

    // Submit button. The Enter key on the password field is the
    // primary path; the button is a fallback for keyboard-less
    // boot situations (touch / fingerprint readers with no kbd).
    Rectangle {
      id: submitButton
      objectName: "qdgreeter.submit"
      Layout.fillWidth: true
      height: 44
      radius: Style.radiusXS
      color: submitMouse.pressed ? Qt.darker(Color.mPrimary, 1.3) : Color.mPrimary
      opacity: (controller && controller.busy) ? Style.opacityMedium : Style.opacityFull

      Text {
        anchors.centerIn: parent
        text: "Sign in"
        font.pointSize: Style.fontSizeXL
        color: Color.mOnPrimary
      }

      MouseArea {
        id: submitMouse
        anchors.fill: parent
        enabled: controller ? !controller.busy : false
        cursorShape: Qt.PointingHandCursor
        onClicked: if (controller) controller.submit()
      }
    }

    // Error label — visible only when greetd or the controller has
    // something to say. Status comes from controller.statusMessage
    // (auth_error description, info auth_message text, or local
    // exception string).
    Text {
      objectName: "qdgreeter.status"
      visible: controller ? controller.statusMessage.length > 0 : false
      text: controller ? controller.statusMessage : ""
      color: Color.mError
      font.pointSize: Style.fontSizeL
      Layout.alignment: Qt.AlignHCenter
    }

    BusyIndicator {
      Layout.alignment: Qt.AlignHCenter
      visible: controller ? controller.busy : false
      running: visible
    }
  }
}
