pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "CaptureState.js" as CaptureState

// Live-capture observer for the lock-screen indicators (J28). Same shape as
// SiloEgressService: one scan process, one monitor process that coalesces into
// a scan, one slow safety timer.
//
// The graph is read with `pw-dump` because PipeWire is the widest capture
// observation point qdistro has today (silos get a bind-mounted view of
// admin's pipewire socket and per-session daemons link upward into it). Read
// Services/Qdistro/CaptureState.js first — it documents exactly what this can
// and cannot see, and why no kind is ever reported as "clear".
//
// STATUS: EXPERIMENTAL. qdshell's own WlSessionLock lock screen is the
// DEPRECATED path (qdwin does not implement ext-session-lock; the runtime lock
// surface is qdlocker, which carries the maintained copy of this observer in
// qdlocker/qdlocker/indicators.py). Nothing instantiates this singleton today.
// Known gaps versus qdlocker's copy, to fix before any consumer arrives:
//   * stdout is buffered whole and only then size-checked, instead of being
//     streamed under the cap;
//   * the completion handlers read the CURRENT _launchGen rather than the
//     generation their own process was launched with, so a late exit from a
//     killed scan can pair with the next scan's stdout (a truncated pair fails
//     the parse, which is why this is a defect and not a live hole);
//   * device-only evidence is not labelled as unattributed in the UI;
//   * `Stopping` silos are not counted as live egress by SiloEgress.js.
Singleton {
  id: root

  // A reading older than this is not evidence of anything: every kind falls
  // back to "unverified" so a wedged observer cannot read as a quiet machine.
  readonly property int staleAfterMs: 15000
  readonly property int pollIntervalMs: 5000
  // A scan that has not finished by then is presumed wedged and killed.
  readonly property int scanTimeoutMs: 4000
  // pw-dump on a sane graph is well under a megabyte; anything past this is
  // treated as a failed observation rather than parsed on the UI thread.
  readonly property int maxDumpBytes: 8000000

  property bool refreshInFlight: false
  // Age is counted in timer ticks, not wall clock, so a clock correction
  // cannot make a dead observer look fresh (a backwards jump would otherwise
  // extend the trusted window indefinitely).
  property int ageMs: 0
  property bool hasReading: false
  readonly property bool fresh: hasReading && ageMs <= staleAfterMs

  // Bumped on every scan launch and on every markStale(). A scan's output is
  // only accepted while its launch generation is still current, so output from
  // a scan that started before the lock (or before a kill) is discarded rather
  // than stamped as the current reading.
  property int generation: 0
  property int _launchGen: -1
  property var _pendingText: null
  property var _pendingExit: null

  // Last accepted parse result, kept so a freshness expiry can re-derive
  // without a scan.
  property var parsed: ({ ok: false, nodes: [] })

  // Derived state (see CaptureState.summarise).
  property bool observerOk: false
  property var kinds: ({})
  property var activeKinds: []
  property var unverifiedKinds: []
  property int activeCount: 0
  property string activeLabel: ""
  property string activeDetail: ""
  property string unverifiedLabel: ""
  property bool anyActive: false
  property bool anyUnverified: false
  property bool active: false
  property bool indicatorVisible: false

  Component.onCompleted: {
    Logger.i("CaptureStateService", "service started");
    _derive();
    refresh();
    _monitor.running = true;
  }

  // Drop trust in the last reading AND in any scan already in flight. Callers
  // that must not inherit pre-lock state (the lock surface) call this before
  // refresh(), so the indicator reads "unverified" until a scan that started
  // after the lock has landed.
  function markStale() {
    generation++;
    hasReading = false;
    ageMs = 0;
    _pendingText = null;
    _pendingExit = null;
    parsed = { ok: false, nodes: [] };
    _derive();
  }

  function refresh() {
    generation++;
    _launchGen = generation;
    _pendingText = null;
    _pendingExit = null;
    _scan.running = false;
    // `exec` so the kill on timeout reaches pw-dump itself and cannot leave an
    // orphan holding the pipe. No `|| true`: a nonzero exit must stay visible
    // to the exit handler.
    _scan.command = ["sh", "-c",
      "command -v pw-dump >/dev/null 2>&1 || exit 127; exec pw-dump"];
    refreshInFlight = true;
    _scanTimeout.restart();
    _scan.running = true;
  }

  // Accept a completed scan only once BOTH its stdout and its exit status are
  // in, and only while its generation is still current.
  function _tryAccept(gen) {
    if (gen !== generation || gen !== _launchGen)
      return;
    if (_pendingText === null || _pendingExit === null)
      return;
    refreshInFlight = false;
    _scanTimeout.stop();
    const exitCode = _pendingExit;
    const raw = _pendingText;
    _pendingText = null;
    _pendingExit = null;
    if (exitCode !== 0) {
      Logger.w("CaptureStateService", "pw-dump exited", exitCode);
      _fail();
      return;
    }
    if (raw.length > maxDumpBytes) {
      Logger.w("CaptureStateService", "pw-dump output too large", raw.length);
      _fail();
      return;
    }
    const next = CaptureState.parsePwDump(raw);
    if (!next.ok) {
      Logger.w("CaptureStateService", "pw-dump produced no usable graph");
      _fail();
      return;
    }
    parsed = next;
    hasReading = true;
    ageMs = 0;
    _derive();
  }

  // A failed scan drops the reading outright: the state goes to "unverified"
  // immediately rather than letting a previous reading stand.
  function _fail() {
    parsed = { ok: false, nodes: [] };
    hasReading = false;
    _derive();
  }

  function _derive() {
    const s = CaptureState.summarise(parsed, { fresh: fresh, limit: 2 });
    observerOk = s.observerOk;
    kinds = s.kinds;
    activeKinds = s.activeKinds;
    unverifiedKinds = s.unverifiedKinds;
    activeCount = s.activeCount;
    activeLabel = s.activeLabel;
    activeDetail = s.activeDetail;
    unverifiedLabel = s.unverifiedLabel;
    anyActive = s.anyActive;
    anyUnverified = s.anyUnverified;
    active = s.anyActive;
    indicatorVisible = s.visible;
  }

  onFreshChanged: _derive()

  Process {
    id: _scan
    stdout: StdioCollector {
      onStreamFinished: {
        root._pendingText = String(this.text || "");
        root._tryAccept(root._launchGen);
      }
    }
    stderr: StdioCollector {}
    onExited: function (exitCode) {
      root._pendingExit = Number(exitCode);
      root._tryAccept(root._launchGen);
    }
  }

  // A scan that never finishes must not hold the indicator in a "last good
  // reading" state: kill it and let the reading age out.
  Timer {
    id: _scanTimeout
    interval: root.scanTimeoutMs
    repeat: false
    onTriggered: {
      if (!root.refreshInFlight)
        return;
      Logger.w("CaptureStateService", "pw-dump timed out; killing scan");
      root.generation++;
      root.refreshInFlight = false;
      root._pendingText = null;
      root._pendingExit = null;
      _scan.running = false;
      root._fail();
    }
  }

  // pw-mon prints on graph changes (node added/removed/state change), so a
  // capture starting while locked is picked up without waiting for the poll.
  // Output is chatty, hence the coalescing timer.
  Process {
    id: _monitor
    running: false
    command: ["sh", "-c",
      "command -v pw-mon >/dev/null 2>&1 || exit 1; exec pw-mon -N -o -a"]
    stdout: SplitParser {
      onRead: data => {
        if (String(data || "").trim() !== "")
          _coalesce.restart();
      }
    }
    stderr: StdioCollector {}
    onExited: function () {
      _restartMonitor.start();
    }
  }

  Timer {
    id: _coalesce
    interval: 300
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: _poll
    interval: root.pollIntervalMs
    repeat: true
    running: true
    onTriggered: root.refresh()
  }

  // Drives the (tick-counted) age used for freshness.
  Timer {
    id: _ageTick
    interval: 1000
    repeat: true
    running: true
    onTriggered: {
      if (root.hasReading)
        root.ageMs += interval;
    }
  }

  Timer {
    id: _restartMonitor
    interval: 5000
    repeat: false
    onTriggered: _monitor.running = true
  }
}
