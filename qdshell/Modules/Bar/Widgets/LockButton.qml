import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Widgets

// Lock-screen trigger widget.
//
// Talks to qdlocker (the systemd-user service) via its ctrl socket at
// $XDG_RUNTIME_DIR/qdlocker.sock — NOT via PanelService.lockScreen
// (the deprecated WlSessionLock path that qdwin doesn't implement).
// The qdwin compositor itself handles Ctrl+Alt+L at the keybind layer
// (qdwin.c qdwin_on_lock_key) and routes lock_requested(reason=3) to
// qdlocker; this button is the panel-trigger equivalent that fires
// lock_requested(reason=3=manual) over the same path.
Item {
  id: root

  implicitWidth: 40
  implicitHeight: 40

  Process {
    id: lockProc
    // socat sends a single newline-terminated "lock" command to the
    // qdlocker ctrl socket. The protocol echoes "ok\n" on success;
    // we discard the response.
    command: ["sh", "-c",
              "printf 'lock\\n' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/qdlocker.sock"]
    stdout: StdioCollector { id: lockStdout }
    stderr: StdioCollector { id: lockStderr }
  }

  MouseArea {
    id: lockMouseArea
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    // Reject anything other than left-click so a fat-finger right-click
    // or a synthetic wl_pointer event with a non-left button can't
    // trigger an unexpected lock.
    acceptedButtons: Qt.LeftButton

    onClicked: lockProc.running = true

    NIconButton {
      anchors.centerIn: parent
      icon: "lock"
      baseSize: 20
      colorBg: lockMouseArea.containsMouse ? Color.mPrimary : "transparent"
      colorFg: lockMouseArea.containsMouse ? Color.mOnPrimary : Color.mOnSurfaceVariant
      tooltipText: "Lock Screen"
    }
  }
}
