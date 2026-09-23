pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// Vault-unlock client API for the qdistro password manager (spec/13).
//
// Wraps the qdistro-pwd daemon's D-Bus surface so qdshell QML code can
// drive vault unlock / lock / rotate / status without speaking dbus
// directly. No UI here — this singleton is the API a future vault UI
// (e.g. a settings tab listing vaults + per-app password reveal popups)
// would consume.
//
// **Scope clarification (vs the impl plan).** The impl plan's Lock.qml
// proposed gating *desktop screen unlock* through broker.UnlockVault.
// That signature didn't match the actual broker — qdistro_pwd_daemon's
// UnlockVault is per-vault password-manager unlock, not a session-PAM
// replacement. Phase 5 redefinition (todo/phase5-prereqs-survey-260503.md)
// scoped Lock.qml to vault-only and left desktop screen unlock as
// Quickshell-native (PAM via login1). If session-unlock-via-broker
// becomes a real feature, that's Phase 5b: a new broker UnlockSession
// method, not a redefinition of UnlockVault.
//
// D-Bus surface (system bus):
//   bus  = org.qdistro.Pwd1
//   path = /org/qdistro/Pwd1
//   sigs = ListVaults() -> as
//          IsUnlocked(s name) -> b
//          UnlockVault(s name, s secret) -> b
//          UnlockVaultFprint(s name, s secret) -> b
//          RotateVault(s name, s old, s new) -> b
//
// All callers must be prepared for the daemon to be absent: on a
// stock qdshell install without qdistro infra, every method short-
// circuits to a "daemon-absent" outcome and the caller's UI should
// reflect that (e.g. hide the vault tab).

Singleton {
  id: root

  readonly property string pwdBus: "org.qdistro.Pwd1"
  readonly property string pwdPath: "/org/qdistro/Pwd1"
  readonly property string pwdIface: "org.qdistro.Pwd1"

  // Cached daemon-presence flag, refreshed by listVaults() (the
  // canonical probe). Until first probe, presumed false so UIs
  // don't flash.
  property bool daemonPresent: false

  // Most recent vault list returned by listVaults(). Use for binding.
  property var vaults: []

  // Signal helpers for UI consumers that prefer event-driven flow.
  signal unlockResult(string name, bool ok, string error)
  signal lockResult(string name, bool ok, string error)
  signal rotateResult(string name, bool ok, string error)
  signal vaultsRefreshed(var names)

  // ---- Public API ------------------------------------------------

  // Refresh the cached vaults[] list.
  function listVaults() {
    _runCall("ListVaults", "", [], (verdict, parsedOut) => {
      if (verdict === "ok") {
        root.daemonPresent = true;
        const arr = _parseStringArray(parsedOut);
        root.vaults = arr;
        root.vaultsRefreshed(arr);
      } else {
        root.daemonPresent = false;
        root.vaults = [];
        root.vaultsRefreshed([]);
      }
    });
  }

  // Synchronous-feel check: emits unlockResult(name, ok, error). Either
  // ok=true (unlocked) or ok=false with a human-readable error.
  function unlockVault(name, secret) {
    _runCall("UnlockVault", "ss", [name, secret], (verdict, parsedOut) => {
      _emitBoolResult(unlockResult, name, verdict, parsedOut);
    });
  }

  function unlockVaultFprint(name, secret) {
    _runCall("UnlockVaultFprint", "ss", [name, secret], (verdict, parsedOut) => {
      _emitBoolResult(unlockResult, name, verdict, parsedOut);
    });
  }

  function rotateVault(name, oldSecret, newSecret) {
    _runCall("RotateVault", "sss", [name, oldSecret, newSecret], (verdict, parsedOut) => {
      _emitBoolResult(rotateResult, name, verdict, parsedOut);
    });
  }

  // Synchronous accessor — only meaningful right after a successful
  // unlockVault() call. Caller must assume false if uncertain.
  function isUnlockedHint(name) {
    return _knownUnlocked.indexOf(name) !== -1;
  }

  // ---- Internals -------------------------------------------------

  property var _knownUnlocked: []

  // Per-call queue, keyed by an integer id. busctl can't hand back
  // user data, so we serialize through a single Process and rely on
  // the env trick used in HooksGate.
  property var _pending: ({})
  property int _nextId: 1

  function _runCall(method, sig, args, callback) {
    const id = _nextId++;
    _pending[id] = {
      "method": method,
      "callback": callback,
    };

    let cmd = ["busctl", "--system", "--no-pager", "call",
               root.pwdBus, root.pwdPath, root.pwdIface, method];
    if (sig && sig.length > 0) {
      cmd.push(sig);
      for (let i = 0; i < args.length; i++) {
        cmd.push(String(args[i]));
      }
    }
    _callProc.command = cmd;
    _callProc.environment = ["__QDSHELL_LOCK_ID=" + id];
    _callProc.running = true;
  }

  function _emitBoolResult(sig, name, verdict, parsedOut) {
    if (verdict === "ok") {
      const ok = (parsedOut === "b true") || /^b\s+true/.test(parsedOut);
      if (ok && _knownUnlocked.indexOf(name) === -1) {
        _knownUnlocked.push(name);
      }
      sig(name, ok, "");
    } else if (verdict === "daemon-absent") {
      sig(name, false, "qdistro-pwd daemon not available");
    } else {
      sig(name, false, parsedOut || "broker call failed");
    }
  }

  function _parseStringArray(out) {
    // busctl prints as e.g. `as 3 "vault1" "vault2" "vault3"`. Strip
    // type signature + count + extract quoted entries.
    if (!out) {
      return [];
    }
    const matches = out.match(/"([^"]*)"/g) || [];
    return matches.map(s => s.slice(1, -1));
  }

  Process {
    id: _callProc
    running: false

    stdout: StdioCollector {
      id: _stdout
    }
    stderr: StdioCollector {
      id: _stderr
    }

    onExited: (exitCode, exitStatus) => {
      let id = -1;
      const env = _callProc.environment || [];
      for (let i = 0; i < env.length; i++) {
        const kv = env[i];
        if (kv.indexOf("__QDSHELL_LOCK_ID=") === 0) {
          id = parseInt(kv.substring("__QDSHELL_LOCK_ID=".length), 10);
          break;
        }
      }
      const entry = (id > 0) ? root._pending[id] : null;
      if (entry && id > 0) {
        delete root._pending[id];
      }
      if (!entry) {
        Logger.w("Lock", "exit handler with no matching pending entry");
        return;
      }

      const stdout = (_stdout.text || "").trim();
      const stderr = (_stderr.text || "").trim();
      let verdict = "ok";
      let payload = stdout;
      if (exitCode !== 0) {
        // ServiceUnknown / NameHasNoOwner / connect-failed → daemon
        // not running. Anything else → propagate as error string.
        if (stderr.indexOf("ServiceUnknown") !== -1
            || stderr.indexOf("NameHasNoOwner") !== -1
            || stderr.indexOf("not provided by any") !== -1
            || stderr.indexOf("Failed to connect to bus") !== -1) {
          verdict = "daemon-absent";
        } else {
          verdict = "error";
          payload = stderr;
        }
      }
      try {
        entry.callback(verdict, payload);
      } catch (e) {
        Logger.e("Lock", "callback raised:", e);
      }
    }
  }

  // Probe daemon presence at startup so UIs that bind on
  // daemonPresent get a quick first answer.
  Component.onCompleted: {
    Logger.i("Lock", "Service started");
    Qt.callLater(listVaults);
  }
}
