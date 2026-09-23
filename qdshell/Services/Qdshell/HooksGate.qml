pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "BrokerGate.js" as BrokerGate

// Defense-in-depth gate around HooksService's script execution.
//
// HooksService already gates execution behind Settings.data.hooks.enabled
// (user-side opt-in). HooksGate adds a second layer: each event-script
// pair is checked against the qdistro broker's CheckPermission rules
// engine before execDetached actually fires, so admin can disable a
// specific hook (e.g. "screenLock" hook running an unwanted shutdown
// command) without touching the user's qdshell settings file.
//
// Action namespace: "hook.allowed:<eventName>"
//   - eventName ∈ { wallpaperChange, darkModeChange, screenLock,
//                   screenUnlock, performanceModeEnabled,
//                   performanceModeDisabled, session, startup }
//
// Details dict: { "script": <full command string> }
//
// Only an explicit, well-formed allow executes. Unknown decisions queue an
// admin request for a future invocation; that request does not authorize this
// invocation. Transport errors and malformed replies deny.
//
// The broker is on the system bus:
//   bus  = org.qdistro.AdminBroker1
//   path = /org/qdistro/AdminBroker1
//   sig  = CheckPermission(s action, a{sv} details) -> s

Singleton {
  id: root

  readonly property string brokerBus: "org.qdistro.AdminBroker1"
  readonly property string brokerPath: "/org/qdistro/AdminBroker1"
  readonly property string brokerIface: "org.qdistro.AdminBroker1"

  property var _queue: []
  property var _active: null

  function gate(event, script, onAllow, onDeny) {
    if (!event || !script) {
      if (onDeny)
        onDeny();
      return;
    }
    if (_queue.length >= 64) {
      Logger.w("HooksGate", "gate queue full; hook denied", event);
      if (onDeny)
        onDeny();
      return;
    }
    _queue.push({ event: event, script: script, onAllow: onAllow, onDeny: onDeny, phase: "check" });
    _startNext();
  }

  function gateBlocking(event, script, onAllow, onDeny) {
    gate(event, script, onAllow, onDeny);
  }

  function _startNext() {
    if (_active || !_queue.length)
      return;
    _active = _queue.shift();
    _checkProcess.command = [
      "busctl", "--system", "--no-pager", "--timeout=2s", "call",
      brokerBus, brokerPath, brokerIface,
      _active.phase === "check" ? "CheckPermission" : "RequestPermission", "sa{sv}",
      "hook.allowed:" + _active.event,
      "1", "script", "s", _active.script
    ];
    _checkProcess.running = true;
  }

  function _finishCheck(exitCode, output) {
    const entry = _active;
    if (entry && entry.phase === "check") {
      const result = BrokerGate.parseStringVerdict(exitCode, output);
      if (result.verdict === "allow") {
        try {
          entry.onAllow();
        } catch (e) {
          Logger.e("HooksGate", "onAllow callback raised:", e);
        }
      } else {
        Logger.w("HooksGate", "hook denied", entry.event, result.reason);
        if (entry.onDeny) {
          try {
            entry.onDeny();
          } catch (e) {
            Logger.e("HooksGate", "onDeny callback raised:", e);
          }
        }
        if (exitCode === 0 && String(output || "").trim() === 's "unknown"' && _queue.length < 64) {
          _queue.push({ event: entry.event, script: entry.script, phase: "request" });
        }
      }
    }
    _active = null;
    Qt.callLater(root._startNext);
  }

  Process {
    id: _checkProcess
    running: false
    stdout: StdioCollector { id: _stdoutCollector }
    stderr: StdioCollector { id: _stderrCollector }
    onExited: (exitCode, exitStatus) => root._finishCheck(exitCode, _stdoutCollector.text)
  }
}
