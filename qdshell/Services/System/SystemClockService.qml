pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import "SystemClock.js" as SystemClock
import qs.Commons

// SystemClockService — system date/time/timezone/NTP control via
// systemd-timedated (`timedatectl`), for the Settings > Region > System Clock
// section. Mirrors the timezone / automatic-time / manual-clock controls of
// xfce4 + GNOME date/time panels.
//
// This service owns OS state (the system clock), NOT a qdshell setting — so
// nothing here is persisted into Settings.data. We read the live state from
// `timedatectl show` and write it back with `timedatectl set-*`.
//
// SECURITY / correctness:
//   * `set-*` requires polkit/root. We run them as a Process (NOT execDetached)
//     so we can observe the exit code: a non-zero exit / polkit denial sets
//     `lastError` to "denied" and we re-read the real state — we NEVER show a
//     change as applied when timedatectl rejected it. (Mirrors the broker rule:
//     do not show a setting as successful if logind/polkit rejects it.)
//   * Injection safety: the timezone and manual datetime are validated in the
//     pure SystemClock.js module — the timezone MUST be an exact member of the
//     enumerated `list-timezones` set, and the datetime must pass a strict
//     regex + range check. The resulting command is a fully-tokenised argv
//     array (no `sh -c`), so a crafted value stays an inert single argv token.
Singleton {
  id: root

  // ─── Capability flags ────────────────────────────────────────────
  // Whether `timedatectl` is present. When false the whole section is gated and
  // we never claim to apply anything.
  property bool hasTimedatectl: false
  // True once the first capability probe has finished.
  property bool capabilitiesReady: false
  readonly property bool available: capabilitiesReady && hasTimedatectl

  // ─── Live system-clock state (from `timedatectl show`) ────────────
  property string timezone: ""
  property bool ntp: false
  property bool ntpSynchronized: false
  property bool localRTC: false
  property bool canNTP: true
  property string timeUSec: ""
  // True once the first state read has completed.
  property bool stateReady: false

  // ─── Enumerated timezone list (the validation allow-list) ─────────
  // Authoritative set from `timedatectl list-timezones`. Used both to populate
  // the picker and as the injection gate for set-timezone.
  property var timezones: []
  property bool timezonesLoaded: false

  // ─── Apply result surface ─────────────────────────────────────────
  // "" = idle/ok, "denied" = polkit/permission rejection, "rejected" = the
  // value failed local validation (never sent), "error" = other failure. The
  // UI binds to this to show a clear "not authorized" state and NEVER reports
  // success on a rejected call.
  property string lastError: ""
  // Human-friendly detail for the error (stderr tail), informational only.
  property string lastErrorDetail: ""
  // Set true while a set-* command is in flight (UI can disable controls).
  property bool applying: false

  signal stateChanged

  // ─── Initialisation ──────────────────────────────────────────────
  function init() {
    Logger.i("SystemClock", "Service started");
    queryCapabilities();
  }

  Component.onCompleted: {
    // Self-init so the singleton works even if shell.qml does not call init().
    queryCapabilities();
  }

  function queryCapabilities() {
    if (capProc.running || capabilitiesReady)
      return;
    capProc.running = true;
  }

  // ─── Capability detection ────────────────────────────────────────
  Process {
    id: capProc
    running: false
    command: ["sh", "-c", "command -v timedatectl >/dev/null 2>&1 && echo yes || echo no"]
    stdout: StdioCollector {
      onStreamFinished: {
        root.hasTimedatectl = String(text || "").trim() === "yes";
        root.capabilitiesReady = true;
        Logger.d("SystemClock", "hasTimedatectl=" + root.hasTimedatectl);
        if (root.hasTimedatectl) {
          root.refresh();
          root.loadTimezones();
        }
      }
    }
    stderr: StdioCollector {}
  }

  // ─── Read live state (`timedatectl show`) ─────────────────────────
  // Use the machine-readable `show` (KEY=VALUE) output, NOT the human `status`
  // output, so parsing is stable across locales/systemd versions.
  Process {
    id: showProc
    running: false
    command: ["timedatectl", "show"]
    property string _buf: ""
    onStarted: _buf = ""
    stdout: SplitParser {
      onRead: data => showProc._buf += data + "\n"
    }
    onExited: (code, status) => {
      if (code === 0) {
        root._applyShow(showProc._buf);
      } else {
        Logger.w("SystemClock", "timedatectl show failed, exit=" + code);
      }
      root.stateReady = true;
    }
    stderr: StdioCollector {}
  }

  function refresh() {
    if (!hasTimedatectl)
      return;
    showProc.running = true;
  }

  function _applyShow(text) {
    var st = SystemClock.parseShow(text);
    root.timezone = st.timezone;
    root.ntp = st.ntp;
    root.ntpSynchronized = st.ntpSynchronized;
    root.localRTC = st.localRTC;
    root.canNTP = st.canNTP;
    root.timeUSec = st.timeUSec;
    Logger.d("SystemClock", "state: tz=" + st.timezone + " ntp=" + st.ntp + " synced=" + st.ntpSynchronized);
    root.stateChanged();
  }

  // ─── Enumerate timezones (`timedatectl list-timezones`) ───────────
  Process {
    id: tzListProc
    running: false
    command: ["timedatectl", "list-timezones"]
    property string _buf: ""
    onStarted: _buf = ""
    stdout: SplitParser {
      onRead: data => tzListProc._buf += data + "\n"
    }
    onExited: (code, status) => {
      if (code === 0) {
        root.timezones = SystemClock.parseTimezones(tzListProc._buf);
        root.timezonesLoaded = true;
        Logger.d("SystemClock", "loaded " + root.timezones.length + " timezones");
      } else {
        Logger.w("SystemClock", "timedatectl list-timezones failed, exit=" + code);
      }
    }
    stderr: StdioCollector {}
  }

  function loadTimezones() {
    if (!hasTimedatectl)
      return;
    tzListProc.running = true;
  }

  // ─── Apply: a single set-* command runner ─────────────────────────
  // Driven by setting `command` then `running = true`. The exit code is
  // inspected on completion: 0 => success (re-read state), non-zero => surface
  // a permission/error state and re-read the real state so the UI never shows a
  // change that did not take effect.
  Process {
    id: applyProc
    running: false
    property string _stderr: ""
    onStarted: _stderr = ""
    stderr: StdioCollector {
      onStreamFinished: applyProc._stderr = text
    }
    stdout: StdioCollector {}
    onExited: (code, status) => {
      root.applying = false;
      if (code === 0) {
        root.lastError = "";
        root.lastErrorDetail = "";
        Logger.i("SystemClock", "set command succeeded");
      } else {
        var err = String(applyProc._stderr || "");
        // polkit / authorization rejection surfaces a distinct UI state.
        if (/not authorized|Interactive authentication required|Access denied|Permission denied/i.test(err)) {
          root.lastError = "denied";
        } else {
          root.lastError = "error";
        }
        root.lastErrorDetail = err.trim();
        Logger.w("SystemClock", "set command failed (exit " + code + "): " + err.trim());
      }
      // Always re-read the real OS state — on failure this reverts the UI to
      // the unchanged value; on success it confirms the new value.
      root.refresh();
    }
  }

  function _runApply(argv) {
    root.lastError = "";
    root.lastErrorDetail = "";
    root.applying = true;
    applyProc.command = argv;
    applyProc.running = true;
  }

  // ─── Public: set timezone ──────────────────────────────────────────
  // SECURITY: the zone is validated against the enumerated list-timezones set
  // in the pure module; anything not an exact member is rejected and NO command
  // runs.
  function setTimezone(tz) {
    if (!available) {
      Logger.w("SystemClock", "setTimezone ignored: timedatectl unavailable");
      return;
    }
    var argv = SystemClock.buildSetTimezoneArgv(tz, timezones);
    if (argv === null) {
      root.lastError = "rejected";
      root.lastErrorDetail = "Timezone not in installed zone list: " + tz;
      Logger.w("SystemClock", "Refusing invalid timezone: " + tz);
      return;
    }
    _runApply(argv);
  }

  // ─── Public: set NTP (automatic time sync) ─────────────────────────
  function setNtp(enabled) {
    if (!available || !stateReady) {
      Logger.w("SystemClock", "setNtp ignored: timedatectl unavailable or state not yet loaded");
      return;
    }
    _runApply(SystemClock.buildSetNtpArgv(enabled === true));
  }

  // ─── Public: set manual date/time ──────────────────────────────────
  // Only meaningful while NTP is OFF (timedatectl refuses set-time with NTP on,
  // which would surface as an error). SECURITY: the datetime string is
  // validated (strict regex + field range check) in the pure module; malformed
  // input is rejected and NO command runs.
  function setTime(datetime) {
    if (!available || !stateReady) {
      Logger.w("SystemClock", "setTime ignored: timedatectl unavailable or state not yet loaded");
      return;
    }
    if (ntp) {
      root.lastError = "rejected";
      root.lastErrorDetail = "Disable automatic time sync (NTP) before setting the clock manually.";
      Logger.w("SystemClock", "Refusing set-time while NTP is on");
      return;
    }
    var argv = SystemClock.buildSetTimeArgv(datetime);
    if (argv === null) {
      root.lastError = "rejected";
      root.lastErrorDetail = "Invalid date/time (expected YYYY-MM-DD HH:MM:SS): " + datetime;
      Logger.w("SystemClock", "Refusing invalid datetime: " + datetime);
      return;
    }
    _runApply(argv);
  }

  // Human-readable name for a timezone (just the zone itself here; the picker
  // shows the raw IANA name, which is what users recognize).
  function clearError() {
    root.lastError = "";
    root.lastErrorDetail = "";
  }
}
