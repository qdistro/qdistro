import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import qs.Commons
import qs.Services.Qdwin
import qs.Widgets
import "../../../../../Services/Qdwin/OutputLayout.js" as OutputLayout

// Display "Layout" subtab — visual monitor arrangement for qdwin's
// wlr-output-management-v1. Drag tiles to position; per-output enable,
// resolution, refresh, scale, rotation, primary; atomic apply with a
// 15-second confirm-or-revert dialog.
//
// ALL layout/validation logic lives in OutputLayout.js (overlap, position
// normalisation, primary selection, mode validation, revert state machine).
// This QML is thin: it mirrors the binding's `outputs` into an editable
// working model, calls into the JS for every decision, and renders.
//
// Untrusted output strings (name/description/make/model) are rendered with
// NText (PlainText by default) and NEVER interpolated into any command.
ColumnLayout {
  id: root
  spacing: Style.marginL
  Layout.fillWidth: true

  // Persist-only note shown when the compositor doesn't advertise the
  // output manager (capability gate false).
  readonly property bool available: CapabilityService.outputManagement

  // Editable working copy of the layout (array of OutputLayout entries).
  property var working: []
  // The serial the working copy was derived from (apply must use it).
  property int baseSerial: 0
  // The baseline to revert to (captured before an apply).
  property var revertBaseline: []
  // Confirm-or-revert phase: "idle" | "applying" | "confirming" | "reverting"
  property string phase: "idle"
  property int confirmSecondsLeft: 15

  // Rebuild the working copy from the live binding snapshot.
  function reload() {
    working = OutputLayout.withPrimary(
      OutputLayout.layoutFromSnapshots(Qdwin.outputs),
      Settings.data.display.primaryOutput || "");
    baseSerial = Qdwin.outputSerial;
    workingChanged();
  }

  // Map of output name -> advertised modes (for validation).
  function modesByName() {
    var m = {};
    var outs = Qdwin.outputs || [];
    for (var i = 0; i < outs.length; i++)
      m[outs[i].name] = outs[i].modes || [];
    return m;
  }

  // Update one entry (by name) with a patch object, re-derive primary.
  // When the patch sets primary:true we clear the flag on every OTHER entry
  // first, so choosePrimary (which returns the first flagged entry in array
  // order) selects the newly-chosen output rather than a stale earlier one.
  function patchEntry(name, patch) {
    var settingPrimary = patch.primary === true;
    var next = [];
    for (var i = 0; i < working.length; i++) {
      var e = working[i];
      var copy = {};
      for (var k in e) copy[k] = e[k];
      if (settingPrimary)
        copy.primary = false;  // clear all; the target is set below
      if (e.name === name)
        for (var p in patch) copy[p] = patch[p];
      next.push(copy);
    }
    working = OutputLayout.withPrimary(next);
    if (settingPrimary)
      Settings.data.display.primaryOutput = OutputLayout.choosePrimary(working);
    workingChanged();
  }

  function validation() {
    return OutputLayout.validateLayout(working, modesByName());
  }

  function applyNow() {
    var norm = OutputLayout.normalizePositions(working);
    working = OutputLayout.withPrimary(norm);
    var v = validation();
    if (!v.ok) {
      ToastService.showWarning(I18n.tr("display.layout.title"),
                               I18n.tr("display.layout.invalid"));
      return;
    }
    // Capture the current live layout as the revert baseline BEFORE applying.
    revertBaseline = OutputLayout.layoutFromSnapshots(Qdwin.outputs);
    phase = "applying";
    var list = OutputLayout.toApplyList(working);
    if (!Qdwin.applyOutputLayout(list, baseSerial)) {
      phase = "idle";
      ToastService.showWarning(I18n.tr("display.layout.title"),
                               I18n.tr("display.layout.apply-failed"));
    }
  }

  function step(event) {
    var s = OutputLayout.nextRevertState(phase, event);
    if (s.revert) {
      // Re-apply the captured baseline against the CURRENT serial (the
      // confirmed apply bumped it). If the revert can't even be submitted
      // (no manager), don't get stuck in "reverting": go straight back to
      // idle and warn. A submitted revert that itself fails/cancels is
      // handled by onOutputLayoutResult's "reverting" → idle transition.
      var list = OutputLayout.toApplyList(revertBaseline);
      if (!Qdwin.applyOutputLayout(list, Qdwin.outputSerial)) {
        ToastService.showWarning(I18n.tr("display.layout.title"),
                                 I18n.tr("display.layout.apply-failed"));
        phase = "idle";
        return;
      }
    }
    phase = s.phase;
  }

  Component.onCompleted: reload()

  Connections {
    target: Qdwin
    function onOutputsChanged() {
      // Re-sync the working copy when not mid-apply (a hotplug or an applied
      // layout changed the live set). During confirming we keep the user's
      // tiles; the live change just re-arms the baseSerial.
      if (root.phase === "idle")
        root.reload();
      else
        root.baseSerial = Qdwin.outputSerial;
    }
    function onOutputLayoutResult(applied, ok, cancelled) {
      if (!applied)
        return;  // a test result; the layout tab only applies
      if (ok) {
        root.step("apply-ok");
      } else {
        root.step("apply-failed");
        ToastService.showWarning(I18n.tr("display.layout.title"),
          cancelled ? I18n.tr("display.layout.cancelled")
                    : I18n.tr("display.layout.apply-failed"));
      }
    }
  }

  // ─── Persist-only banner ────────────────────────────────────────────
  NBox {
    Layout.fillWidth: true
    visible: !root.available
    implicitHeight: Math.round(persistCol.implicitHeight + Style.marginL * 2)
    color: Color.mSurface
    ColumnLayout {
      id: persistCol
      anchors.fill: parent
      anchors.margins: Style.marginL
      NText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: I18n.tr("display.layout.persist-only")
        color: Color.mOnSurfaceVariant
      }
    }
  }

  // ─── Visual arrangement canvas ──────────────────────────────────────
  NLabel {
    label: I18n.tr("display.layout.arrangement")
    description: I18n.tr("display.layout.arrangement-desc")
  }

  NBox {
    Layout.fillWidth: true
    Layout.preferredHeight: 240
    color: Color.mSurfaceVariant

    // Scale global-compositor coordinates into the canvas. Compute the
    // bounding box of the enabled tiles and fit it with margin.
    Item {
      id: canvas
      anchors.fill: parent
      anchors.margins: Style.marginM

      property real fitScale: {
        var maxX = 1, maxY = 1;
        for (var i = 0; i < root.working.length; i++) {
          var e = root.working[i];
          if (!e.enabled) continue;
          var r = OutputLayout.effectiveRect(e);
          maxX = Math.max(maxX, r.x + r.w);
          maxY = Math.max(maxY, r.y + r.h);
        }
        var sx = width / maxX;
        var sy = height / maxY;
        return Math.max(0.01, Math.min(sx, sy)) * 0.92;
      }

      Repeater {
        model: root.working
        delegate: Rectangle {
          id: tile
          required property var modelData
          required property int index
          visible: modelData.enabled
          property var rect: OutputLayout.effectiveRect(modelData)
          width: Math.max(20, rect.w * canvas.fitScale)
          height: Math.max(16, rect.h * canvas.fitScale)
          x: rect.x * canvas.fitScale
          y: rect.y * canvas.fitScale
          radius: Style.radiusS
          color: modelData.primary ? Color.mPrimary : Color.mSurface
          border.color: Color.mOutline
          border.width: 1

          NText {
            anchors.centerIn: parent
            width: parent.width - Style.marginS * 2
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
            // PlainText (default) — output name is untrusted.
            text: modelData.name + (modelData.primary ? " *" : "")
            color: modelData.primary ? Color.mOnPrimary : Color.mOnSurface
            pointSize: Style.fontSizeXS
          }

          MouseArea {
            anchors.fill: parent
            drag.target: tile
            drag.axis: Drag.XAndYAxis
            onReleased: {
              // Convert canvas position back to global coords and patch.
              var gx = Math.round(tile.x / canvas.fitScale);
              var gy = Math.round(tile.y / canvas.fitScale);
              root.patchEntry(tile.modelData.name, { x: gx, y: gy });
            }
          }
        }
      }
    }
  }

  // ─── Per-output controls ────────────────────────────────────────────
  Repeater {
    model: root.working
    delegate: NBox {
      Layout.fillWidth: true
      required property var modelData
      implicitHeight: Math.round(outCol.implicitHeight + Style.marginL * 2)
      color: Color.mSurface

      ColumnLayout {
        id: outCol
        anchors.fill: parent
        anchors.margins: Style.marginL
        spacing: Style.marginM

        RowLayout {
          Layout.fillWidth: true
          NText {
            Layout.fillWidth: true
            // PlainText (default) — name + EDID-derived description untrusted.
            text: modelData.name
                  + (modelData.description && modelData.description !== modelData.name
                     ? "  (" + modelData.description + ")" : "")
            pointSize: Style.fontSizeM
            font.weight: Style.fontWeightSemiBold
            elide: Text.ElideRight
          }
          NToggle {
            label: I18n.tr("display.layout.enabled")
            checked: modelData.enabled
            onToggled: function (c) { root.patchEntry(modelData.name, { enabled: c }); }
          }
        }

        GridLayout {
          Layout.fillWidth: true
          columns: 2
          columnSpacing: Style.marginL
          rowSpacing: Style.marginM
          enabled: modelData.enabled

          // Resolution + refresh combo.
          NComboBox {
            Layout.fillWidth: true
            label: I18n.tr("display.layout.resolution")
            model: {
              var items = [];
              var modes = (function () {
                var outs = Qdwin.outputs || [];
                for (var i = 0; i < outs.length; i++)
                  if (outs[i].name === modelData.name) return outs[i].modes || [];
                return [];
              })();
              for (var i = 0; i < modes.length; i++) {
                var m = modes[i];
                items.push({
                  key: m.width + "x" + m.height + "@" + m.refresh,
                  name: m.width + " x " + m.height
                        + (m.refresh > 0 ? "  " + (m.refresh / 1000).toFixed(2) + " Hz" : "")
                        + (m.preferred ? "  *" : "")
                });
              }
              return items;
            }
            currentKey: modelData.width + "x" + modelData.height + "@" + modelData.refresh
            onSelected: function (key) {
              var parts = key.split(/[x@]/);
              root.patchEntry(modelData.name, {
                width: parseInt(parts[0], 10),
                height: parseInt(parts[1], 10),
                refresh: parseInt(parts[2], 10)
              });
            }
          }

          // Scale.
          NComboBox {
            Layout.fillWidth: true
            label: I18n.tr("display.layout.scale")
            model: [
              { key: "1", name: "100%" },
              { key: "2", name: "200%" },
              { key: "3", name: "300%" }
            ]
            currentKey: String(modelData.scale)
            onSelected: function (key) {
              root.patchEntry(modelData.name, { scale: parseInt(key, 10) });
            }
          }

          // Rotation (transform).
          NComboBox {
            Layout.fillWidth: true
            label: I18n.tr("display.layout.rotation")
            model: [
              { key: "0", name: I18n.tr("display.layout.rotate-0") },
              { key: "1", name: I18n.tr("display.layout.rotate-90") },
              { key: "2", name: I18n.tr("display.layout.rotate-180") },
              { key: "3", name: I18n.tr("display.layout.rotate-270") }
            ]
            currentKey: String(modelData.transform)
            onSelected: function (key) {
              root.patchEntry(modelData.name, { transform: parseInt(key, 10) });
            }
          }

          // Primary.
          NToggle {
            label: I18n.tr("display.layout.primary")
            checked: modelData.primary
            onToggled: function (c) {
              if (c) root.patchEntry(modelData.name, { primary: true });
            }
          }
        }
      }
    }
  }

  // ─── Apply / revert actions ─────────────────────────────────────────
  RowLayout {
    Layout.fillWidth: true
    spacing: Style.marginM

    NButton {
      text: I18n.tr("display.layout.reset")
      outlined: true
      enabled: root.available && root.phase === "idle"
      onClicked: root.reload()
    }
    Item { Layout.fillWidth: true }
    NButton {
      text: I18n.tr("display.layout.apply")
      enabled: root.available && root.phase === "idle"
      onClicked: root.applyNow()
    }
  }

  // ─── Confirm-or-revert dialog ───────────────────────────────────────
  Timer {
    id: confirmTimer
    interval: 1000
    repeat: true
    running: root.phase === "confirming"
    onTriggered: {
      root.confirmSecondsLeft -= 1;
      if (root.confirmSecondsLeft <= 0) {
        running = false;
        root.step("timeout");
      }
    }
  }
  onPhaseChanged: {
    if (phase === "confirming")
      confirmSecondsLeft = 15;
  }

  Dialog {
    id: confirmDialog
    modal: true
    visible: root.phase === "confirming"
    // A Dialog is a popup (overlaid, not laid out by the parent ColumnLayout);
    // centre it on the overlay. Matches the AutostartListSubTab/SessionTab
    // dialog pattern already used across the Settings tabs.
    anchors.centerIn: Overlay.overlay
    title: I18n.tr("display.layout.confirm-title")
    closePolicy: Popup.NoAutoClose

    ColumnLayout {
      spacing: Style.marginL
      NText {
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        text: I18n.tr("display.layout.confirm-body")
              + " (" + root.confirmSecondsLeft + "s)"
      }
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.marginM
        NButton {
          text: I18n.tr("display.layout.revert")
          outlined: true
          onClicked: root.step("cancel")
        }
        Item { Layout.fillWidth: true }
        NButton {
          text: I18n.tr("display.layout.keep")
          onClicked: root.step("confirm")
        }
      }
    }
  }
}
