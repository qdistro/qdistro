// qdlocker root window.
//
// One full-screen QQuickWindow hosting LockUI.qml. qdwin recognizes
// this process as the authorized locker and promotes this Qt toplevel
// to the compositor LOCK layer while locked; pywayland owns only the
// private control protocol.

import QtQuick
import QtQuick.Window
import shim

Window {
  id: root
  width: Screen.width
  height: Screen.height
  // Fullscreen + opaque so the first frame after attach can't show
  // through to whatever toplevel was focused before the lock.
  visibility: bridge.locked ? Window.FullScreen : Window.Hidden
  color: Color.mSurface
  flags: Qt.FramelessWindowHint

  // contextProperty: see app.py's setContextProperty("controller", ...).
  // Reaching for `controller` directly here (rather than
  // `property var controller: controller`) avoids the shadow warning
  // and keeps the LockUI binding explicit.

  LockUI {
    id: lockUI
    anchors.fill: parent
    lockController: controller
  }
}
