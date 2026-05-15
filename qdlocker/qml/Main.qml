// qdlocker root window.
//
// One full-screen QQuickWindow hosting LockUI.qml. The window's
// wl_surface is currently NOT the LOCK-layer surface — pywayland in
// wayland.py owns the lock surface on its own connection. Visual
// rendering and the compositor's lock state live on separate planes
// for now; unifying them needs a small Qt↔pywayland bridge (see
// README §Status).

import QtQuick
import QtQuick.Window

Window {
  id: root
  visible: true
  width: Screen.width
  height: Screen.height
  // Fullscreen + opaque so the first frame after attach can't show
  // through to whatever toplevel was focused before the lock.
  visibility: Window.FullScreen
  color: "#101015"
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
