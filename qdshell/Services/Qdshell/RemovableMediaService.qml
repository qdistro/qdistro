pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.UI
import "RemovableMedia.js" as RemovableMedia

// Removable-media insert/remove notifications + brokered mount/unmount.
//
// SECURITY MODEL (qdistro/doc/removable-media-design.md):
//   - This service NEVER mounts directly and NEVER runs anything off a
//     device. Mount/unmount requests are sent to the root helper
//     qdistro-media-exec over its AF_UNIX socket; the helper
//     authenticates us via SO_PEERCRED and asks the qdistro broker
//     (org.qdistro.AdminBroker1) for permission before doing anything.
//   - Autorun NEVER auto-executes. The strongest auto-action is opening
//     a file manager AT THE MOUNTPOINT DIRECTORY (xdg-open <dir>), which
//     cannot execute a .desktop / binary on the device. Defaults are
//     prompt/ignore.
//   - Device labels are untrusted: they flow into PlainText toast text
//     and the media request's display-only `label` field, never into a
//     shell command or an argv that runs a device-controlled program.
//
// Detection uses `udisksctl monitor`, parsed line-wise. We only act on
// devices with a filesystem (so partition tables / whole disks without a
// fs don't spam). The monitor output is host-trusted structurally (it is
// udisks2's own format) but the values (labels) are still treated as
// untrusted display strings.
Singleton {
  id: root

  readonly property string mediaClientPath: "/usr/local/lib/qdistro/qdistro_media_exec_client.py"
  readonly property string socketPath: "/run/qdistro-media-exec/sock"

  // Resolved live policy from Settings.
  readonly property bool enabled: Settings.data.removableMedia.enabled
  readonly property string mountPolicy: RemovableMedia.normalizeMountPolicy(Settings.data.removableMedia.mountPolicy)
  readonly property string autorunPolicy: RemovableMedia.normalizeAutorunPolicy(Settings.data.removableMedia.autorunPolicy)

  // device path -> { label } cache for remove-time notifications.
  property var _known: ({})

  // -- public API (also unit-testable shape via RemovableMedia.js) -----

  // Ask the root helper to mount `device`. `meta` carries untrusted
  // display fields (label/fstype/uuid). On success optionally opens a
  // file manager at the mountpoint (NEVER executes anything).
  function requestMount(device, meta, openAfter) {
    if (!device)
      return;
    _mountProc._openAfter = Boolean(openAfter);
    _mountProc._label = (meta && meta.label) ? String(meta.label) : "";
    const req = RemovableMedia.buildMediaRequest("mount", device, meta || {});
    _mountProc.command = [_mediaClientArgv0(), mediaClientPath, JSON.stringify(req)];
    _mountProc.running = true;
  }

  function requestUnmount(device, meta) {
    if (!device)
      return;
    const req = RemovableMedia.buildMediaRequest("unmount", device, meta || {});
    _unmountProc.command = [_mediaClientArgv0(), mediaClientPath, JSON.stringify(req)];
    _unmountProc.running = true;
  }

  function _mediaClientArgv0() {
    return "/usr/bin/python3";
  }

  // Open a file manager AT the mountpoint directory. Opens a DIRECTORY
  // path only — xdg-open on a directory launches the file manager; it
  // does not and cannot run an executable/.desktop that lives on the
  // device. Tokenized argv, never a shell string.
  function _openFileManager(mountpoint) {
    if (!mountpoint)
      return;
    Quickshell.execDetached(["xdg-open", String(mountpoint)]);
  }

  // -- insertion handling ----------------------------------------------
  function _onDeviceAdded(device, label, fstype, uuid) {
    if (!root.enabled)
      return;
    root._known[device] = { "label": label || "" };

    if (Settings.data.removableMedia.notifyOnInsert) {
      // Label rendered as plain text in the toast — never interpolated.
      const desc = label ? label : device;
      ToastService.showNotice(I18n.tr("removable-media.inserted-title"), desc, "usb", 6000);
    }

    const decision = RemovableMedia.decideOnInsert(root.mountPolicy, root.autorunPolicy);
    const meta = { "label": label || "", "fstype": fstype || "", "uuid": uuid || "" };
    if (decision.action === "ignore") {
      return;
    } else if (decision.action === "mount") {
      root.requestMount(device, meta, decision.thenOpen === true);
    } else {
      // "prompt": offer Mount as the toast action. The toast offers a
      // single action; the full Mount/Open/Nothing choice set lives in
      // the toast/popup UI. "Nothing" is just dismissing the toast.
      ToastService.showNotice(
        I18n.tr("removable-media.insert-prompt-title"),
        label ? label : device,
        "usb", 12000,
        I18n.tr("removable-media.action-mount"),
        function () {
          root.requestMount(device, meta, root.autorunPolicy === "open");
        });
    }
  }

  function _onDeviceRemoved(device) {
    if (!root.enabled)
      return;
    const cached = root._known[device];
    delete root._known[device];
    if (Settings.data.removableMedia.notifyOnRemove) {
      const desc = (cached && cached.label) ? cached.label : device;
      ToastService.showNotice(I18n.tr("removable-media.removed-title"), desc, "usb", 4000);
    }
  }

  // -- mount/unmount reply handling ------------------------------------
  Process {
    id: _mountProc
    running: false
    property bool _openAfter: false
    property string _label: ""
    stdout: StdioCollector {
      id: _mountStdout
    }
    stderr: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      const reply = RemovableMedia.parseMediaReply(String(_mountStdout.text || ""));
      if (exitCode !== 0 || !reply.ok) {
        ToastService.showWarning(I18n.tr("removable-media.mount-failed-title"),
                                 reply.error || I18n.tr("removable-media.broker-denied"));
        return;
      }
      ToastService.showNotice(I18n.tr("removable-media.mounted-title"),
                              reply.mountpoint, "usb", 5000);
      if (_mountProc._openAfter && reply.mountpoint) {
        root._openFileManager(reply.mountpoint);
      }
    }
  }

  Process {
    id: _unmountProc
    running: false
    stdout: StdioCollector {
      id: _unmountStdout
    }
    stderr: StdioCollector {}
    onExited: (exitCode, exitStatus) => {
      const reply = RemovableMedia.parseMediaReply(String(_unmountStdout.text || ""));
      if (exitCode !== 0 || !reply.ok) {
        ToastService.showWarning(I18n.tr("removable-media.unmount-failed-title"),
                                 reply.error || I18n.tr("removable-media.broker-denied"));
        return;
      }
      ToastService.showNotice(I18n.tr("removable-media.unmounted-title"), "", "usb", 4000);
    }
  }

  // -- udisks2 monitor (detection only; never privileged) --------------
  // `udisksctl monitor` is an unprivileged read-only D-Bus listener. We
  // parse its line stream for filesystem add/remove events. Parsing is
  // intentionally conservative: a line we don't recognise is ignored.
  Process {
    id: _monitor
    running: root.enabled
    command: ["udisksctl", "monitor"]
    stdout: SplitParser {
      onRead: line => root._parseMonitorLine(String(line || ""))
    }
  }

  // Track the "current object" block udisksctl monitor prints. We only
  // surface block devices that expose an IdLabel/IdType (i.e. carry a
  // filesystem). This parser reads structural fields from udisks2's own
  // output; the VALUES (label) remain untrusted display strings.
  property string _curDevice: ""
  property string _curLabel: ""
  property string _curFstype: ""
  property string _curUuid: ""
  property bool _curAdded: false

  function _flushCurrent() {
    if (root._curDevice && root._curFstype) {
      if (root._curAdded)
        root._onDeviceAdded(root._curDevice, root._curLabel, root._curFstype, root._curUuid);
    }
    root._curDevice = "";
    root._curLabel = "";
    root._curFstype = "";
    root._curUuid = "";
    root._curAdded = false;
  }

  function _parseMonitorLine(line) {
    const t = line.trim();
    // New event header lines start with a timestamp + verb; flush the
    // previous block first.
    if (/:\s*(Added|Removed|added|removed)\s/.test(t) || /InterfacesAdded|InterfacesRemoved/.test(t)) {
      root._flushCurrent();
      root._curAdded = /Added|added/.test(t);
      const dm = t.match(/(\/org\/freedesktop\/UDisks2\/block_devices\/\w+)/);
      if (dm)
        root._curDevice = "/dev/" + dm[1].split("/").pop();
      // A removal we can resolve immediately by /dev path.
      if (!root._curAdded && root._curDevice) {
        const removed = root._curDevice;
        root._curDevice = "";
        root._onDeviceRemoved(removed);
      }
      return;
    }
    if (root._curAdded) {
      let m;
      if ((m = t.match(/Device:\s*(\/dev\/\S+)/)))
        root._curDevice = m[1];
      else if ((m = t.match(/IdLabel:\s*(.*)$/)))
        root._curLabel = m[1].trim();
      else if ((m = t.match(/IdType:\s*(\S+)/)))
        root._curFstype = m[1].trim();
      else if ((m = t.match(/IdUUID:\s*(\S+)/)))
        root._curUuid = m[1].trim();
    }
  }

  // Flush any trailing block on a short idle so the last device of a
  // burst is surfaced.
  Timer {
    id: _flushTimer
    interval: 400
    repeat: true
    running: root.enabled
    onTriggered: {
      if (root._curAdded && root._curDevice && root._curFstype)
        root._flushCurrent();
    }
  }
}
