import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Services.System

// Find-cursor pointer highlight.
//
// XFCE parity: flashes an animated ring at the current pointer position when
// the user triggers it (via `qs ipc call findCursor show`, a bound compositor
// shortcut, or the settings "Test" button). Implemented entirely in qdshell —
// no external backend required.
//
// One transparent fullscreen layershell surface per screen is created only for
// the brief duration of the flash, so input is never blocked while idle. While
// shown, a hovering MouseArea tracks the live pointer location and an expanding,
// fading ring is drawn there. The surface is torn down as soon as the animation
// completes.
Variants {
  id: overlays

  // Active only while a flash is in progress; this keeps zero surfaces alive
  // when idle so the overlay never intercepts pointer events.
  property bool active: false

  // Bump on each request so re-triggering mid-flash restarts the animation.
  property int requestSeq: 0

  model: active ? Quickshell.screens : []

  Connections {
    target: AccessibilityService
    function onFindCursorRequested() {
      overlays.requestSeq++;
      overlays.active = true;
    }
  }

  // Safety net: if for some reason a per-screen overlay does not finish (e.g.
  // the pointer is on another output and never enters this one), tear all
  // surfaces down a little after the longest configured animation.
  Timer {
    id: globalCleanup
    interval: Math.max(300, (Settings.data.accessibility.findCursorDurationMs || 700)) + 600
    repeat: false
    running: overlays.active
    onTriggered: overlays.active = false
  }

  delegate: PanelWindow {
    id: win
    required property ShellScreen modelData
    screen: modelData

    readonly property color ringColor: Settings.data.accessibility.findCursorRingColor || "#ff4081"
    readonly property int ringSize: Math.max(40, Settings.data.accessibility.findCursorRingSize || 220)
    readonly property int durationMs: Math.max(150, Settings.data.accessibility.findCursorDurationMs || 700)
    readonly property int seq: overlays.requestSeq

    anchors.top: true
    anchors.bottom: true
    anchors.left: true
    anchors.right: true
    color: "transparent"

    WlrLayershell.namespace: "qdshell-find-cursor-" + (screen?.name || "unknown")
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.exclusionMode: ExclusionMode.Ignore

    // Re-run when the request sequence changes (re-trigger during a flash).
    onSeqChanged: tracker.restart()

    MouseArea {
      id: tracker
      anchors.fill: parent
      hoverEnabled: true
      // We need live hover position to locate the pointer, and qdwin exposes
      // no cursor-position API to read it without a surface. So this surface
      // does receive the pointer for the (very short) flash duration — exactly
      // like the standalone X11 `find-cursor` utility and XFCE's own
      // implementation, which briefly grab the pointer to draw the highlight.
      // We accept no buttons (Qt.NoButton) and propagate composed events so we
      // never act on clicks ourselves; the surface tears itself down the moment
      // the animation ends, keeping the grab to a few hundred milliseconds.
      acceptedButtons: Qt.NoButton
      propagateComposedEvents: true

      // Whether the pointer has been located on this output yet.
      property bool located: false

      function placeAt(x, y) {
        ring.x = x - ring.width / 2;
        ring.y = y - ring.height / 2;
        located = true;
        ring.flash();
      }

      function restart() {
        located = false;
        if (containsMouse)
          placeAt(mouseX, mouseY);
      }

      onEntered: placeAt(mouseX, mouseY)
      onPositionChanged: mouse => {
        if (!located)
          placeAt(mouse.x, mouse.y);
      }

      Component.onCompleted: {
        // If the surface is created with the pointer already over it, entered
        // may not fire — seed from the current hover state on next tick.
        Qt.callLater(function () {
          if (!located && containsMouse)
            placeAt(mouseX, mouseY);
        });
      }
    }

    // The animated highlight ring.
    Item {
      id: ring
      width: win.ringSize
      height: win.ringSize
      visible: tracker.located
      opacity: 0
      scale: 0.1

      function flash() {
        flashAnim.stop();
        flashAnim.start();
      }

      Rectangle {
        anchors.fill: parent
        radius: width / 2
        color: "transparent"
        border.color: win.ringColor
        border.width: Math.max(3, Math.round(parent.width * 0.04))
      }

      // Inner pulse for extra visibility.
      Rectangle {
        anchors.centerIn: parent
        width: parent.width * 0.4
        height: width
        radius: width / 2
        color: Qt.alpha(win.ringColor, 0.25)
        border.color: win.ringColor
        border.width: Math.max(2, Math.round(parent.width * 0.02))
      }

      SequentialAnimation {
        id: flashAnim
        ParallelAnimation {
          NumberAnimation {
            target: ring
            property: "opacity"
            from: 0.0
            to: 1.0
            duration: Math.round(win.durationMs * 0.25)
            easing.type: Easing.OutQuad
          }
          NumberAnimation {
            target: ring
            property: "scale"
            from: 0.1
            to: 1.0
            duration: Math.round(win.durationMs * 0.45)
            easing.type: Easing.OutBack
          }
        }
        NumberAnimation {
          target: ring
          property: "opacity"
          from: 1.0
          to: 0.0
          duration: Math.round(win.durationMs * 0.55)
          easing.type: Easing.InQuad
        }
        ScriptAction {
          script: {
            ring.scale = 0.1;
            // This output's flash is done; if every surface is gone the
            // Variants model collapses. Deactivating here is safe because the
            // pointer can only be over one output at a time.
            overlays.active = false;
          }
        }
      }
    }
  }
}
