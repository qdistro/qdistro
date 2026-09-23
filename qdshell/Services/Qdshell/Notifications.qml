pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// Passive audit forwarder for FreeDesktop notifications.
//
// Per-silo routing is already handled by the host topology: each silo
// runs its own qdshell instance binding org.freedesktop.Notifications
// on its own session bus. NotificationService.qml stays unchanged as
// the FreeDesktop server. This singleton only fires-and-forgets a
// system-bus call to broker.RecordNotification(app, summary, body,
// urgency) so admin's audit-log gets a per-uid-tagged trail of what
// was shown.
//
// Why a singleton instead of inline busctl in NotificationService:
//   - NotificationService is a 1162-LoC Noctalia file we'd rather not
//     keep diverging from upstream — one call site is the minimum
//     diff, the rest of the logic stays here.
//   - Failure-handling (broker absent, rate-limit, malformed reply)
//     is centralized.
//
// Broker absent → silently no-op. qdshell must work as a stock shell
// without any qdistro infra installed.

Singleton {
  id: root

  readonly property string brokerBus: "org.qdistro.AdminBroker1"
  readonly property string brokerPath: "/org/qdistro/AdminBroker1"
  readonly property string brokerIface: "org.qdistro.AdminBroker1"

  // After three consecutive failures, suppress further calls for the
  // session — broker is clearly absent or unreachable; further attempts
  // just burn busctl forks. Re-enable on next qdshell start.
  property int _consecFailures: 0
  readonly property int _failureThreshold: 3
  property bool _suppressed: false

  // Public API — call once per notification accepted by NotificationService.
  // The notification object follows Quickshell's Notifications service
  // shape: appName / summary / body / urgency.
  function audit(notification) {
    if (_suppressed) {
      return;
    }
    if (!notification) {
      return;
    }
    const app = String(notification.appName || "");
    const summary = String(notification.summary || "");
    const body = String(notification.body || "");
    const urgency = (typeof notification.urgency === "number") ? notification.urgency : 1;

    _auditProc.command = [
      "busctl", "--system", "--no-pager", "call",
      brokerBus, brokerPath, brokerIface,
      "RecordNotification", "sssi",
      app, summary, body, String(urgency),
    ];
    _auditProc.running = true;
  }

  Process {
    id: _auditProc
    running: false

    // Suppress stdout/stderr unless we hit the failure threshold.
    stdout: StdioCollector {
      id: _auditStdout
    }
    stderr: StdioCollector {
      id: _auditStderr
    }

    onExited: (exitCode, exitStatus) => {
      if (exitCode === 0) {
        root._consecFailures = 0;
        return;
      }
      root._consecFailures += 1;
      if (root._consecFailures >= root._failureThreshold) {
        root._suppressed = true;
        Logger.d("Notifications",
                 "broker.RecordNotification unreachable after "
                 + root._failureThreshold
                 + " attempts; suppressing further audits this session");
      }
    }
  }
}
