pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import Qdistro.Qdwin 1.0
import qs.Commons
import qs.Services.Control
import qs.Services.Qdshell
import qs.Services.UI
import "../Qdshell/BrokerGate.js" as BrokerGate
import "../Qdshell/ClipboardSilo.js" as ClipboardSilo
import "RemoteMachine.js" as RemoteMachine

/// qdshell Qdwin — qdwin-only.
///
/// In upstream Noctalia this service detects the host compositor
/// (Hyprland / Niri / Sway / Mango / Labwc) at startup and loads a
/// matching backend adapter to provide workspace + window data via
/// per-compositor IPC. qdshell drops the adapters because we run on
/// exactly one compositor (qdwin via libweston). The foreign-compositor
/// identity flags are gone too — `isQdwin` is the only backend identity.
///
/// The QML interface (workspaces ListModel, focus helpers, session
/// controls, spawn) is preserved so the consumer .qml files compile
/// unchanged. As of 2026-05-14 the `windows`
/// ListModel + focus driving are populated via `Qdistro.Qdwin`
/// (libqdistro-qdwin.so QML plugin) which binds qdwin_shell_v1 at v14
/// and exposes toplevel events + imperative requests to QML. Workspace
/// data stays empty (qdwin doesn't expose workspaces yet). Session
/// controls (lock/suspend/etc.) still shell out to `loginctl`/
/// `systemctl` — those don't need compositor IPC.
Singleton {
    id: root

    // qdwin is the only supported compositor; this is the sole backend
    // identity flag. (Kept readonly to make accidental writes fail loudly.)
    readonly property bool isQdwin: true

    // Output (display) management. qdwin implements wlr-output-management-v1;
    // the binding enumerates heads/modes (outputs) and applies an atomic
    // layout (applyOutputLayout). These proxy the live binding so the Display
    // layout tab (and CapabilityService.outputManagement) can consume them
    // without reaching into the internal qdwinBinding id. outputManagement-
    // Available gates the capability on the manager actually being advertised.
    readonly property bool outputManagementAvailable:
        qdwinBinding ? qdwinBinding.outputManagementAvailable : false
    readonly property var outputs: qdwinBinding ? qdwinBinding.outputs : []
    readonly property int outputSerial:
        qdwinBinding ? qdwinBinding.outputSerial : 0

    // Workspace state. As of v24 qdwin has real workspaces, exposed via
    // the standard ext-workspace-v1 protocol and surfaced by the
    // QdwinBinding plugin (workspaceCount / activeWorkspace). The count is
    // owned by the user (qdshell settings) and reconciled down to the
    // compositor via setWorkspaceCount. v27: per-workspace names are ALSO
    // pushed down (setWorkspaceName) so qdwin echoes them on the standard
    // ext_workspace_handle_v1.name event to EVERY ext-workspace client
    // (waybar etc.), not just qdshell's local overlay — capability-gated on
    // a >= v27 shell bind (older compositors just show positional names; the
    // local overlay below still renders the user's names in qdshell). When
    // the binding is not yet bound we fall back to the settings count so
    // the bar still renders cells. Occupancy is derived from the windows
    // model's per-window workspaceId (toplevel_workspace sidecar).
    property ListModel workspaces: ListModel {}
    property int _settingsWorkspaceCount: Settings.isLoaded ? Settings.data.workspaces.count : 4
    property var _settingsWorkspaceNames: Settings.isLoaded ? Settings.data.workspaces.names : []
    // One-shot guard: count reconciliation runs once per bind, on the first
    // workspacesChanged that carries a live count. Re-armed on every (re)bind.
    property bool _wsCountPushed: false

    on_SettingsWorkspaceCountChanged: {
        // Push the user's desired count down to the compositor, then
        // rebuild (the live count refreshes again on workspacesChanged).
        if (qdwinBinding && qdwinBinding.bound)
            qdwinBinding.setWorkspaceCount(_settingsWorkspaceCount);
        _pushWorkspaceNames();
        _rebuildWorkspaces();
    }
    on_SettingsWorkspaceNamesChanged: {
        // v27: also propagate the user's names to the compositor so every
        // ext-workspace client (not just qdshell) sees them. No-op against
        // an older shell (the binding gates on its negotiated version).
        _pushWorkspaceNames();
        _rebuildWorkspaces();
    }

    // Settings.data is a nested JS object.  Mutating workspaces.count from a
    // settings control persists the value, but it does not reliably notify a
    // QML binding that reads the nested member.  Keep this explicit entry
    // point at the UI boundary so a user count change reaches qdwin
    // immediately instead of waiting for a shell restart/rebind.
    function applyWorkspaceCount(count) {
        var desired = Math.max(1, Math.min(Math.round(Number(count)), 32));
        var names = (Settings.data.workspaces.names || []).slice();
        while (names.length < desired)
            names.push(String(names.length + 1));
        if (names.length > desired)
            names = names.slice(0, desired);

        Settings.data.workspaces.count = desired;
        Settings.data.workspaces.names = names;
        _settingsWorkspaceNames = names;
        _settingsWorkspaceCount = desired;

        // Assigning the same value does not fire the property handler.  Make
        // the live update idempotent so every explicit UI action is applied.
        if (qdwinBinding && qdwinBinding.bound)
            qdwinBinding.setWorkspaceCount(desired);
        _pushWorkspaceNames();
        _rebuildWorkspaces();
    }

    // v27: push every workspace's custom display name down to the compositor
    // via qdwin_shell_v1.set_workspace_name, so qdwin re-advertises them on
    // the standard ext_workspace_handle_v1.name event. An index with no
    // custom name (or an empty one) is sent as "" — the compositor reverts
    // that workspace to its positional default. Bounded to the live/settings
    // count so we never push past the workspaces qdwin actually has.
    function _pushWorkspaceNames() {
        if (!qdwinBinding || !qdwinBinding.bound) return;
        if (qdwinBinding.setWorkspaceName === undefined) return;  // < v27 plugin
        var names = _settingsWorkspaceNames || [];
        var desired = Math.max(1, Math.min(_settingsWorkspaceCount, 32));
        var live = qdwinBinding.workspaceCount > 0 ? qdwinBinding.workspaceCount : 0;
        var count = Math.max(desired, live);
        for (var i = 0; i < count; i++) {
            var nm = (i < names.length && names[i] !== undefined) ? names[i] : "";
            qdwinBinding.setWorkspaceName(i, nm);
        }
    }

    function _rebuildWorkspaces() {
        if (!Settings.isLoaded) return;
        // Prefer the compositor's live workspace count + active index;
        // fall back to the settings count until the binding is up.
        var bound = qdwinBinding && qdwinBinding.bound && qdwinBinding.workspaceCount > 0;
        var count = bound ? qdwinBinding.workspaceCount
                          : Math.max(1, Math.min(_settingsWorkspaceCount, 32));
        var active = bound ? qdwinBinding.activeWorkspace : 0;
        var names = _settingsWorkspaceNames || [];
        // Occupancy: which workspaces currently hold at least one window.
        var occupied = {};
        for (var w = 0; w < windows.count; w++)
            occupied[windows.get(w).workspaceId] = true;
        workspaces.clear();
        for (var i = 0; i < count; i++) {
            var label = (i < names.length && names[i] !== "") ? names[i] : String(i + 1);
            workspaces.append({
                id: i,
                idx: i + 1,
                name: label,
                output: "",
                isFocused: i === active,
                isActive: i === active,
                isUrgent: false,
                isOccupied: occupied[i] === true,
            });
        }
        root.workspaceChanged();
    }

    // Window state is populated from qdwin_shell_v1 events via the
    // Qdistro.Qdwin plugin (see QdwinBinding below).
    //
    // Each row carries: handle, ownerUid, appId, title, isXwayland,
    // workspaceId, sandboxEngine, secctxAppId, instanceId.
    // The latter three default to "" and are filled in when the
    // wp_security_context_v1 tag arrives (toplevel_security_context
    // event fires after toplevel_added). PodApps / VMApps services
    // use secctxAppId + instanceId for placeholder correlation.
    property ListModel windows: ListModel {}
    property int focusedWindowIndex: -1
    readonly property bool overviewActive: false
    readonly property bool globalWorkspaces: true

    // Alt+Tab switcher state. Once we bind qdwin_shell_v1 at v14+,
    // qdwin stops driving alt+tab focus itself — it emits
    // `switcher_next(dir)` to the bound shell on each Tab press while
    // Alt is held, then `switcher_commit` on Alt release. The shell
    // is expected to walk a candidate list and call set_keyboard_focus
    // on the commit. We keep a tiny ring-buffer position; nothing
    // fancier than wrap-around is required for parity with the v0
    // qdwin-internal switcher.
    property int _switcherIndex: -1

    // qdwin_shell_v1 binding. Constructed eagerly so the v14 bind
    // happens at qdshell startup — needed for the qdwin focus-emit /
    // keybinding branches to fire (their fallback "unbound" log path
    // runs while no shell is bound). The binding takes no QML
    // properties; we drive it via signal handlers + Q_INVOKABLE
    // methods.
    // External-facing wrappers for the native binding's Q_INVOKABLE
    // methods. Exposed so peer singletons (e.g. Tier3FocusIPC) and
    // IPC handlers can drive qdwin without needing direct access to
    // the internal qdwinBinding id.
    function injectFocus(handle, seat) {
        if (!qdwinBinding) return;
        qdwinBinding.focusWindow(handle, seat || "default");
        Logger.i("Qdwin", "ipc injectFocus handle=" + handle
                 + " seat=" + (seat || "default"));
    }
    function clearSeatSelection(seat, isPrimary) {
        if (!qdwinBinding) return;
        qdwinBinding.clearSelection(seat || "default", isPrimary ? 1 : 0);
        Logger.i("Qdwin", "ipc clearSelection seat=" + (seat || "default")
                 + " primary=" + (isPrimary ? 1 : 0));
    }
    // P05a Phase A: per-toplevel chrome colour. Tier4Apps / Tier3Apps
    // call this after resolving a toplevel's silo so qdwin stores the
    // rgba per-handle (qdwin_toplevel_border_rgba in qdwin.c). Pre-P05a
    // the rgba arg was logged + dropped on the qdwin side; now the SSD
    // paint helper reads it back via the per-toplevel state. Returns
    // nothing — fire-and-forget. Logs on no-binding so a race during
    // shell startup leaves a journal trace.
    function setBorderColor(handle, rgba) {
        if (!qdwinBinding) {
            Logger.w("Qdwin", "setBorderColor handle=" + handle
                              + " rgba=" + rgba + " — no binding");
            return;
        }
        qdwinBinding.setBorderColor(handle, rgba >>> 0);
    }

    // Output (display) management wrappers. `layout` is the QVariantList the
    // OutputLayout.toApplyList() helper builds; `serial` must be the current
    // root.outputSerial (a stale serial is rejected by the compositor as
    // `cancelled`). apply is ATOMIC + reversible: on failure/cancel the
    // compositor reverts, and the Display tab re-applies the saved baseline.
    // The async verdict arrives via root.outputLayoutResult.
    signal outputLayoutResult(bool applied, bool ok, bool cancelled)
    signal outputLayoutTaggedResult(string tag, bool ok, bool cancelled)
    signal remoteOutputInputResult(string outputName, bool enabled, bool applied)
    signal remoteOutputDrainResult(string outputName, bool applied)
    function applyOutputLayout(layout, serial) {
        if (!qdwinBinding || !qdwinBinding.outputManagementAvailable) {
            Logger.w("Qdwin", "applyOutputLayout — no output manager");
            return false;
        }
        return qdwinBinding.applyLayout(layout, serial >>> 0);
    }
    function testOutputLayout(layout, serial) {
        if (!qdwinBinding || !qdwinBinding.outputManagementAvailable)
            return false;
        return qdwinBinding.testLayout(layout, serial >>> 0);
    }
    function applyOutputLayoutTagged(layout, serial, tag) {
        if (!qdwinBinding || !qdwinBinding.outputManagementAvailable
                || !tag || tag.length > 128)
            return false;
        return qdwinBinding.applyLayoutTagged(layout, serial >>> 0, tag);
    }

    function _windowByHandle(handle) {
        for (let i = 0; i < root.windows.count; i++) {
            const row = root.windows.get(i);
            if (row.handle === handle)
                return row;
        }
        return null;
    }

    function _siloForWindow(row) {
        if (!row)
            return "unknown";
        const secctxSilo = ClipboardSilo.fromSecctx(
            row.sandboxEngine || "",
            row.secctxAppId || "",
            row.instanceId || "");
        if (secctxSilo.length > 0)
            return secctxSilo;
        if (typeof row.ownerUid === "number")
            return "uid:" + row.ownerUid;
        return "unknown";
    }

    function _verifyWindowIdentity(row) {
        if (!row || !qdwinBinding || qdwinBinding.verifyClientIdentity === undefined)
            return false;
        if (!row.peerPid || row.peerPid <= 0)
            return false;
        return qdwinBinding.verifyClientIdentity(
            row.peerPid >>> 0,
            row.peerStarttime || 0,
            row.peerUid >>> 0,
            row.peerExe || "",
            row.peerSelinuxLabel || "",
            row.sandboxEngine || "",
            row.secctxAppId || "",
            row.instanceId || "");
    }

    function _verifyActivationIdentity(sourceRow, targetRow, sourceSilo, targetSilo) {
        if (sourceSilo === targetSilo && sourceSilo.indexOf("uid:") === 0
                && sourceRow && targetRow
                && sourceRow.ownerUid === targetRow.ownerUid)
            return true;
        return root._verifyWindowIdentity(sourceRow)
            && root._verifyWindowIdentity(targetRow);
    }

    function _decideNestedProxy(handle, appId, originUid) {
        const action = BrokerGate.nestedProxyAction(appId);
        let decision = { verdict: "deny", reason: "broker-unavailable" };
        if (qdwinBinding && qdwinBinding.checkPermission !== undefined) {
            const result = qdwinBinding.checkPermission(
                action, BrokerGate.nestedProxyDetails(appId, originUid));
            decision = BrokerGate.parseStringVerdict(
                result.exitCode, result.stdout || "", "broker-unavailable");
        }
        Logger.i("Qdwin", "NESTED_PROXY_GATE",
                 "handle=" + handle,
                 "app_id=" + (appId || ""),
                 "origin_uid=" + originUid,
                 "verdict=" + decision.verdict,
                 "reason=" + decision.reason);
        qdwinBinding.nestedProxyDecision(
            handle, BrokerGate.qdwinDecision(decision.verdict),
            decision.reason);
    }

    function _decideActivation(handle, sourceHandle, targetHandle, sourceAppId) {
        const sourceRow = sourceHandle !== 4294967295
            ? root._windowByHandle(sourceHandle) : null;
        const targetRow = root._windowByHandle(targetHandle);
        const sourceSilo = root._siloForWindow(sourceRow);
        const targetSilo = root._siloForWindow(targetRow);
        let decision = { verdict: "deny", reason: "unknown-identity" };

        if (BrokerGate.knownSilo(sourceSilo) && BrokerGate.knownSilo(targetSilo)
                && qdwinBinding
                && qdwinBinding.checkHandoffActivation !== undefined) {
            const srcApp = sourceAppId || (sourceRow ? (sourceRow.secctxAppId || sourceRow.appId || "") : "");
            const dstApp = targetRow ? (targetRow.secctxAppId || targetRow.appId || "") : "";
            const srcEngine = sourceRow ? (sourceRow.sandboxEngine || "") : "";
            const identityVerified = root._verifyActivationIdentity(
                sourceRow, targetRow, sourceSilo, targetSilo);
            // Relay the source app's authenticated (pid, starttime) so the
            // broker attests the source silo via its launch-record store
            // (P1-1). 0/0 when the source row has no peer identity → broker
            // enforce denies cross-silo rather than trusting the claim.
            const result = qdwinBinding.checkHandoffActivation(
                sourceSilo, targetSilo, srcApp, dstApp, srcEngine,
                identityVerified,
                sourceRow ? (sourceRow.peerPid >>> 0) : 0,
                sourceRow ? (sourceRow.peerStarttime || 0) : 0);
            decision = BrokerGate.parseStringVerdict(
                result.exitCode, result.stdout || "", "broker-unavailable");
        }

        Logger.i("Qdwin", "ACTIVATION_GATE",
                 "handle=" + handle,
                 "src_handle=" + sourceHandle,
                 "target_handle=" + targetHandle,
                 "src_silo=" + sourceSilo,
                 "dst_silo=" + targetSilo,
                 "src_app=" + (sourceAppId || ""),
                 "verdict=" + decision.verdict,
                 "reason=" + decision.reason);
        qdwinBinding.activationDecision(
            handle, BrokerGate.qdwinDecision(decision.verdict),
            decision.reason);
    }

    IpcHandler {
        target: "qdwin"

        function closeWindow(handle: int): void {
            root.closeHandle(handle);
        }

        function focusWindow(handle: int): void {
            root.focusWindow(handle);
        }

        function positionWindow(handle: int, x: int, y: int): void {
            root.requestSetPositionHandle(handle, x, y);
        }

        function lastOverlayKeys(): string {
            return "count=" + (qdwinBinding ? qdwinBinding.overlayKeyCount : 0);
        }

        function capabilities(): string {
            return "bound=" + (!!(qdwinBinding && qdwinBinding.bound))
                + " version=" + (qdwinBinding ? qdwinBinding.shellVersion : 0)
                + " wmPolicy=" + (qdwinBinding ? (qdwinBinding.shellVersion >= 25) : false)
                + " keybindRegistration=" + (qdwinBinding ? (qdwinBinding.shellVersion >= 25) : false)
                // v26: the real derived idle/DPMS capability (a >= v26 bind AND
                // ext_idle_notifier_v1 + a wl_seat) — the same CapabilityService
                // state that drives the `idleDpms -> true` log, so the read is
                // deterministic instead of racing a transient journal line.
                + " idleDpms=" + CapabilityService.idleDpms;
        }
    }

    QdwinBinding {
        id: qdwinBinding

        onBoundChanged: {
            if (bound) {
                Logger.i("Qdwin", "qdwin_shell_v1 bound v" + shellVersion);
                // spec/10 Phase-1 — wire the clipboard gate now that
                // we have a live binding. ClipboardGate.init is
                // idempotent so re-binds after a teardown are safe.
                ClipboardGate.init(qdwinBinding);
                // P05a: a tier-4 toplevel that appeared *before* the
                // binding landed had its setBorderColor() call dropped
                // (no binding → logged + returned), so it sits with
                // neutral chrome. Now that we are bound, notify peers so
                // Tier4Apps can replay the per-toplevel border paint for
                // any pre-bind windows. Fires only on the false→true
                // transition (QdwinBinding.bound flips false→true once
                // per bind), so no replay spam.
                root.shellBound();
                // v24: workspace-count reconciliation is deferred to the
                // first workspacesChanged (the ext-workspace manager/handles
                // may not have arrived yet at hello time — setWorkspaceCount
                // would no-op). Re-arm the one-shot on every (re)bind.
                root._wsCountPushed = false;
                // v27: (re)assert the user's workspace names on every bind so
                // a fresh/restarted compositor re-learns them (it only keeps
                // them for its own lifetime). No-op against a < v27 shell.
                root._pushWorkspaceNames();
                root._rebuildWorkspaces();
                // v25: window-manager policy + WM-shortcut keybinds are live
                // once the shell binds at >= v25 (set_wm_policy / the wired
                // v19 register_hotkey). Gate the capability on the actual
                // bind version, mirroring outputManagement — an older
                // compositor leaves the WindowManager tab persist-only.
                CapabilityService.setWmPolicy(shellVersion >= 25);
                CapabilityService.setKeybindRegistration(shellVersion >= 25);
                // v28: live pointer (set_pointer_config) and key-repeat
                // (set_key_repeat) config — the Mouse and Keyboard tabs apply
                // live once the shell binds at >= v28. PointerInputService /
                // KeyboardInputService route through the binding when these
                // flip true and fall back to persist-only otherwise.
                CapabilityService.setPointerConfig(shellVersion >= 28);
                CapabilityService.setXkbRepeat(shellVersion >= 28);
                // v26: idle/DPMS needs both the v26 set_display_power request
                // and the ext-idle-notify client (notifier + seat). The latter
                // arrives via its own global, so also re-evaluate on
                // idleNotifierAvailable change below.
                root._refreshIdleDpmsCapability();
            } else {
                // Unbound: the compositor can no longer apply WM policy or
                // hold our hotkeys, so drop the capability (the tab reverts
                // to persist-only until the next bind).
                CapabilityService.setWmPolicy(false);
                CapabilityService.setKeybindRegistration(false);
                CapabilityService.setIdleDpms(false);
                CapabilityService.setPointerConfig(false);
                CapabilityService.setXkbRepeat(false);
                if (lastError.length > 0)
                    Logger.w("Qdwin", "qdwin_shell_v1 unbound: " + lastError);
            }
        }
        // v24: ext-workspace state changed (active index or count). On the
        // first event with a live count, reconcile the compositor count to
        // the user's persisted setting exactly once (issuing all needed
        // create/remove in one batch avoids the overshoot that re-running
        // it on every intermediate done would cause). Then rebuild the bar.
        onWorkspacesChanged: {
            if (!root._wsCountPushed && Settings.isLoaded
                    && qdwinBinding.workspaceCount > 0) {
                root._wsCountPushed = true;
                if (qdwinBinding.workspaceCount !== root._settingsWorkspaceCount)
                    qdwinBinding.setWorkspaceCount(root._settingsWorkspaceCount);
                // v27: the handle set is now live — (re)push names so the
                // newly-created handles carry the user's names too.
                root._pushWorkspaceNames();
            }
            root._rebuildWorkspaces();
        }
        // v24 sidecar: a window's workspace assignment. Update the row and
        // refresh occupancy.
        onToplevelWorkspace: (handle, index) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "workspaceId", index);
                    break;
                }
            }
            root._rebuildWorkspaces();
        }
        // Per-window state bitmask (QDWIN_TS_*: 1=maximized, 2=fullscreen,
        // 4=minimized, …). Tracked so the WM toggle-maximize / toggle-
        // fullscreen shortcuts can flip the *current* state of the focused
        // window (request_maximize / request_fullscreen are absolute, not
        // toggles).
        onToplevelState: (handle, state) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "state", state >>> 0);
                    break;
                }
            }
        }
        // v19/v25: a registered WM-shortcut hotkey fired — relay the id so
        // WindowManagerService can map it to a window-manager action on the
        // focused window.
        onHotkeyPressed: (id) => root.hotkeyPressed(id)
        onLastErrorChanged: {
            if (lastError.length > 0)
                Logger.w("Qdwin", "binding error: " + lastError);
        }
        // Output (display) management: relay the compositor's apply/test
        // verdict up to the Display layout tab's confirm-or-revert path.
        onLayoutResult: (applied, ok, cancelled) => {
            root.outputLayoutResult(applied, ok, cancelled);
        }
        onLayoutTaggedResult: (tag, ok, cancelled) => {
            root.outputLayoutTaggedResult(tag, ok, cancelled);
        }
        onRemoteOutputInputResult: (outputName, enabled, applied) => {
            root.remoteOutputInputResult(outputName, enabled, applied);
        }
        onRemoteOutputDrainResult: (outputName, applied) => {
            root.remoteOutputDrainResult(outputName, applied);
        }
        // Gate CapabilityService.outputManagement on the manager actually
        // being advertised. Fires on bind (manager appears), hotplug, and
        // disconnect (manager gone → false).
        onOutputsChanged: {
            CapabilityService.setOutputManagement(
                qdwinBinding.outputManagementAvailable);
        }
        // v26: ext_idle_notifier_v1 + wl_seat became (un)available — re-derive
        // the idle/DPMS capability (which also needs a >= v26 shell bind).
        onIdleNotifierAvailableChanged: root._refreshIdleDpmsCapability()
        // v26: an armed idle notification fired — relay to PowerService.
        onIdleStateChanged: (slot, idle) => root.idleStateChanged(slot, idle)
        onLauncherRequested: {
            const screen = PanelService.findScreenForPanels();
            if (screen)
                PanelService.toggleLauncher(screen);
            else
                Logger.w("Qdwin", "launcher_requested with no available screen");
        }

        onToplevelAdded: (handle, ownerUid, appId, title, isXwayland) => {
            root.windows.append({
                handle: handle,
                ownerUid: ownerUid,
                appId: appId || "",
                title: title || "",
                isXwayland: isXwayland,
                workspaceId: 0,
                state: 0,
                sandboxEngine: "",
                secctxAppId: "",
                instanceId: "",
                peerPid: 0,
                peerStarttime: 0,
                peerUid: 0,
                peerExe: "",
                peerSelinuxLabel: "",
                remoteNestedAuthorized: false,
                remoteSourceMachine: "",
                remoteTrustDomainId: "",
                remoteStreamId: "",
                remoteGeneration: 0,
            });
            root.windowListChanged();
        }
        onToplevelSecurityContext: (handle, sandboxEngine, secctxAppId, instanceId) => {
            // Receive-side log line. Mirrors qdwin's send-side line at
            // qdwin/qdwin.c:814 ("qdwin: toplevel_security_context …")
            // so the wire path is greppable from both ends — the
            // load-bearing assertion in
            // tests/integration/vm/s41-secctx-toplevel-event.sh.
            Logger.i("Qdwin", "toplevel_security_context handle=" + handle
                + " engine=" + (sandboxEngine || "")
                + " app_id=" + (secctxAppId || "")
                + " instance=" + (instanceId || ""));
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "sandboxEngine", sandboxEngine || "");
                    root.windows.setProperty(i, "secctxAppId",   secctxAppId   || "");
                    root.windows.setProperty(i, "instanceId",    instanceId    || "");
                    root.windowSecctxResolved(handle, sandboxEngine || "",
                                              secctxAppId || "", instanceId || "");
                    return;
                }
            }
        }
        onToplevelPeerIdentity: (handle, peerPid, peerStarttime, peerUid, peerExe, peerSelinuxLabel) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "peerPid", peerPid >>> 0);
                    root.windows.setProperty(i, "peerStarttime", peerStarttime);
                    root.windows.setProperty(i, "peerUid", peerUid >>> 0);
                    root.windows.setProperty(i, "peerExe", peerExe || "");
                    root.windows.setProperty(i, "peerSelinuxLabel", peerSelinuxLabel || "");
                    return;
                }
            }
        }
        onNestedProxyRemoteIdentity: (handle, sourceMachine, trustDomainId, streamId, generation) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "remoteSourceMachine", sourceMachine || "");
                    root.windows.setProperty(i, "remoteTrustDomainId", trustDomainId || "");
                    root.windows.setProperty(i, "remoteStreamId", streamId || "");
                    root.windows.setProperty(i, "remoteGeneration", generation);
                    root.windows.setProperty(i, "remoteNestedAuthorized", true);
                    Logger.i("Qdwin", "nested_proxy_remote_identity handle=" + handle
                        + " source=" + sourceMachine + " trust_domain=" + trustDomainId
                        + " stream=" + streamId + " generation=" + generation);
                    root.windowListChanged();
                    return;
                }
            }
        }
        onNestedProxyPending: (handle, appId, originUid) => {
            root._decideNestedProxy(handle, appId, originUid);
        }
        onActivationPending: (handle, sourceHandle, targetHandle, sourceAppId) => {
            root._decideActivation(handle, sourceHandle, targetHandle, sourceAppId);
        }
        onNestedProxyPixelSource: (handle, pwNode, inputSink) => {
            // qdwin is asking for a pixel-consumer process. Spawn
            // qdistro-nested-pixelfeed; it connects back to the outer
            // wayland, creates a wl_surface, and calls bind_proxy_pixels.
            // Until it does, the proxy view stays on the placeholder
            // curtain (see qdwin-shell-v1.xml nested_proxy_pixel_source).
            // The consumer process self-detaches; we don't track it.
            if (!pwNode || pwNode.length === 0) {
                Logger.w("Qdwin", "nested_proxy_pixel_source: empty pw_node for handle " + handle);
                return;
            }
            let argv;
            if (pwNode.startsWith("qdistro.remote:")) {
                // R6 remote sources name only a local random rendezvous token.
                // Reject path/shell syntax before selecting the dedicated
                // decoder-owned SHM feeder; the token becomes a fixed
                // XDG_RUNTIME_DIR socket name inside that root-installed binary.
                if (!/^qdistro\.remote:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(pwNode)) {
                    Logger.w("Qdwin", "invalid remote pixel token for handle " + handle);
                    return;
                }
                argv = ["/usr/bin/qdistro-mm-remote-pixelfeed",
                          String(handle), pwNode];
            } else {
                // The daemon's dmabuf lane is still an explicitly documented
                // diagnostic path: backend-pipewire can crash the nested Weston
                // after format negotiation. Keep production local nesting on
                // SHM until that producer bug has a live reliability gate.
                argv = ["/usr/bin/env", "QDWIN_PIXELFEED_NO_DMABUF=1",
                          "/usr/bin/qdistro-nested-pixelfeed",
                          String(handle), pwNode];
                if (inputSink && inputSink.length > 0) argv.push(inputSink);
            }
            Logger.i("Qdwin", "spawning pixelfeed for handle " + handle
                              + " pw_node=" + pwNode);
            Quickshell.execDetached(argv);
        }
        onToplevelRemoved: (handle) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.remove(i);
                    if (root.focusedWindowIndex === i) {
                        root.focusedWindowIndex = -1;
                        root.activeWindowChanged();
                    } else if (root.focusedWindowIndex > i) {
                        root.focusedWindowIndex -= 1;
                    }
                    root.windowListChanged();
                    // v24: a closed window may have emptied its workspace —
                    // refresh per-workspace occupancy. (No toplevel_workspace
                    // sidecar fires on removal, so rebuild here.)
                    root._rebuildWorkspaces();
                    return;
                }
            }
        }
        onToplevelTitle: (handle, title) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "title", title || "");
                    if (i === root.focusedWindowIndex)
                        root.activeWindowChanged();
                    return;
                }
            }
        }
        onToplevelAppId: (handle, appId) => {
            for (let i = 0; i < root.windows.count; i++) {
                if (root.windows.get(i).handle === handle) {
                    root.windows.setProperty(i, "appId", appId || "");
                    if (i === root.focusedWindowIndex)
                        root.activeWindowChanged();
                    root.windowListChanged();
                    return;
                }
            }
        }
        onSeatFocusChanged: (seat, handle) => {
            // Match on handle; UINT32_MAX (=4294967295) means "no focus".
            let next = -1;
            if (handle !== 4294967295) {
                for (let i = 0; i < root.windows.count; i++) {
                    if (root.windows.get(i).handle === handle) { next = i; break; }
                }
            }
            if (next !== root.focusedWindowIndex) {
                root.focusedWindowIndex = next;
                root.activeWindowChanged();
            }
        }

        onSwitcherNext: (dir) => {
            if (root.windows.count === 0) return;
            if (root._switcherIndex < 0)
                root._switcherIndex = root.focusedWindowIndex;
            const n = root.windows.count;
            root._switcherIndex =
                ((root._switcherIndex + dir) % n + n) % n;
        }
        onSwitcherCommit: () => {
            if (root._switcherIndex >= 0
                && root._switcherIndex < root.windows.count) {
                qdwinBinding.focusWindow(
                    root.windows.get(root._switcherIndex).handle);
            }
            root._switcherIndex = -1;
        }
    }

    // Display scales: persisted via ShellState. qdwin will publish
    // updates over qdwin_shell_v1.output_*; until then we just load
    // whatever the user persisted last.
    property var displayScales: ({})
    property bool displayScalesLoaded: false

    property var backend: null  // never assigned; consumers default-check

    signal workspaceChanged
    signal activeWindowChanged
    signal windowListChanged
    // Emitted INSTEAD of request_close for multi-machine remote (qdistro.mm.*)
    // toplevels: their close is SOURCE-mediated (impl-34 Q3). RemoteMachineWindows
    // routes it to the in-VM broker's RequestClose; the window stays visible until
    // the source emits Closed. NEVER followed by qdwinBinding.closeWindow here
    // (that xdg-closes FreeRDP = the forbidden client-tree kill; qdwin also
    // refuses request_close for these as a compositor backstop).
    signal remoteCloseRequested(int handle)
    // Fires when wp_security_context_v1 fields arrive for a known
    // toplevel. PodApps / VMApps services listen here to resolve
    // their cold-start placeholders by instanceId match.
    signal windowSecctxResolved(int handle, string sandboxEngine,
                                string secctxAppId, string instanceId)
    // Fires when qdwin_shell_v1 transitions to bound (false→true). Tier
    // chrome services (Tier4Apps) listen here to replay per-toplevel
    // border paint for windows that appeared before the binding landed
    // — their setBorderColor() calls were dropped while unbound.
    signal shellBound
    // v25: a registered WM-shortcut hotkey fired. `id` is the shell-assigned
    // token passed to registerHotkey(); WindowManagerService maps it back to
    // a window-manager action on the focused window. Relayed from the binding.
    signal hotkeyPressed(int id)
    // v26: an armed idle notification changed state (idle=true on idled,
    // false on resumed). `slot` is the PowerService slot. Relayed from the
    // ext-idle-notify client in the binding.
    signal idleStateChanged(int slot, bool idle)

    // v26: whether the ext-idle-notify client + a wl_seat are bound (one half
    // of the idle/DPMS capability; the other is a >= v26 shell bind).
    readonly property bool idleNotifierAvailable: qdwinBinding ? qdwinBinding.idleNotifierAvailable : false

    // Re-derive CapabilityService.idleDpms from the two requirements: a
    // >= v26 shell bind (set_display_power) AND the ext-idle-notify source.
    function _refreshIdleDpmsCapability() {
      const ok = qdwinBinding && qdwinBinding.bound
               && qdwinBinding.shellVersion >= 26
               && qdwinBinding.idleNotifierAvailable;
      CapabilityService.setIdleDpms(!!ok);
    }

    // v25: compositor-focused toplevel handle (0 = none). Source of truth for
    // the WM keyboard shortcuts, which act on whatever window currently holds
    // keyboard focus. The binding reports UINT32_MAX (=4294967295) for "no
    // focus" — normalise that (and any non-positive) to 0 so consumers only
    // ever see a live handle or 0, never a sentinel passed into a v25 request.
    readonly property int focusedHandle: {
      if (!qdwinBinding)
        return 0;
      const h = qdwinBinding.focusedHandle;
      return (h <= 0 || h === 4294967295) ? 0 : h;
    }

    Component.onCompleted: {
        Qt.callLater(() => {
            if (typeof ShellState !== 'undefined' && ShellState.isLoaded) {
                loadDisplayScalesFromState();
            }
            // Populate workspaces on startup (live compositor count if the
            // binding is already up, else the settings fallback).
            if (Settings.isLoaded) {
                _rebuildWorkspaces();
            }
        });
    }

    Connections {
        target: Settings
        function onSettingsLoaded() {
            if (qdwinBinding && qdwinBinding.bound)
                qdwinBinding.setWorkspaceCount(root._settingsWorkspaceCount);
            root._pushWorkspaceNames();
            root._rebuildWorkspaces();
        }
    }

    Connections {
        target: typeof ShellState !== 'undefined' ? ShellState : null
        function onIsLoadedChanged() {
            if (ShellState.isLoaded)
                loadDisplayScalesFromState();
        }
    }

    function loadDisplayScalesFromState() {
        try {
            const cached = ShellState.getDisplay();
            if (cached && Object.keys(cached).length > 0) {
                displayScales = cached;
            }
            displayScalesLoaded = true;
        } catch (error) {
            Logger.e("Qdwin", "Failed to load display scales:", error);
            displayScalesLoaded = true;
        }
    }

    function getDisplayScale(outputName) {
        return displayScales[outputName] || 1.0;
    }

    // -- workspace + window queries -- //

    function getActiveWorkspaces() {
        const result = [];
        for (let i = 0; i < workspaces.count; i++) {
            const ws = workspaces.get(i);
            if (ws.isActive) result.push(ws);
        }
        return result;
    }

    function getWindowsForWorkspace(workspaceId) {
        const result = [];
        for (let i = 0; i < windows.count; i++) {
            const w = windows.get(i);
            if (w.workspaceId === workspaceId) result.push(w);
        }
        return result;
    }

    function getFocusedWindow() {
        return focusedWindowIndex >= 0 && focusedWindowIndex < windows.count
            ? windows.get(focusedWindowIndex)
            : null;
    }

    function getFocusedWindowTitle() {
        const w = getFocusedWindow();
        return w ? (w.title || "") : "";
    }

    function getFocusedScreen() {
        // Fall back to first connected screen — qdwin doesn't yet
        // publish "focused output". Consumers that need precise
        // focus should query qdwin_shell_v1 directly.
        return Quickshell.screens.length > 0 ? Quickshell.screens[0] : null;
    }

    // -- workspace + window actions -- //
    // Wired through Qdistro.Qdwin → qdwin_shell_v1. `window` is either
    // a row from the `windows` ListModel (has .handle) or a bare
    // numeric handle; we accept both so callers don't have to wrap.

    // Switch the active workspace. Accepts a workspace model row (has
    // .id == 0-based index, or .idx == 1-based), or a bare numeric index.
    // Drives ext-workspace-v1 via the binding; the active cell updates
    // when the resulting workspacesChanged fires.
    function switchToWorkspace(workspace) {
        var idx = -1;
        if (typeof workspace === "number")
            idx = workspace;
        else if (workspace && workspace.id !== undefined)
            idx = workspace.id;
        else if (workspace && workspace.idx !== undefined)
            idx = workspace.idx - 1;
        if (idx < 0) return;
        if (qdwinBinding && qdwinBinding.bound)
            qdwinBinding.activateWorkspace(idx);
    }

    // Move a window to a workspace ("send to workspace N"). `window` is a
    // windows-model row (has .handle) or a bare numeric handle.
    function moveToWorkspace(window, index) {
        const h = _handleOf(window);
        if (h < 0 || index < 0) return;
        if (qdwinBinding && qdwinBinding.bound)
            qdwinBinding.moveToplevelToWorkspace(h, index);
    }

    function _handleOf(w) {
        if (w === null || w === undefined) return -1;
        if (typeof w === "number") return w;
        if (typeof w === "object" && "handle" in w) return w.handle;
        return -1;
    }

    function focusWindow(window) {
        const h = _handleOf(window);
        if (h < 0) return;
        qdwinBinding.focusWindow(h);
    }

    function closeWindow(window) {
        const h = _handleOf(window);
        if (h < 0) return;
        // Multi-machine remote (qdistro.mm.*) toplevels: close is SOURCE-mediated
        // (impl-34 Q3). Resolve the secctx app_id for this handle (object caller
        // or bare-handle Taskbar/Workspace caller) and, if it is a remote-machine
        // window, emit remoteCloseRequested INSTEAD of request_close and RETURN —
        // never xdg-close the FreeRDP client (the forbidden client-tree kill).
        // RemoteMachineWindows routes it to the broker's RequestClose; teardown
        // waits for the source Closed.
        let mmSecctx = "";
        if (typeof window === "object" && window !== null
                && typeof window.secctxAppId === "string")
            mmSecctx = window.secctxAppId;
        if (!mmSecctx) {
            for (let i = 0; i < root.windows.count; i++) {
                const w0 = root.windows.get(i);
                if (w0.handle === h && typeof w0.secctxAppId === "string") {
                    mmSecctx = w0.secctxAppId;
                    break;
                }
            }
        }
        // Use isManagedRemote (prefix AND a parseable origin+stream), not the
        // bare prefix: a malformed qdistro.mm.* id would be intercepted here but
        // dropped by RemoteMachineWindows (which needs both), black-holing close
        // (codex impl-36 MED). A malformed mm window instead falls through; the
        // qdwin compositor guard still refuses request_close for engine=qdistro.mm
        // (a safe no-op), so it is never xdg-closed.
        if (RemoteMachine.isManagedRemote(mmSecctx)) {
            root.remoteCloseRequested(h);
            return;     // NO qdwinBinding.closeWindow — source-mediated close
        }
        // P05a: if this is a tier-4 VM toplevel, route the close button
        // through the per-VM Tier4VM.Control.Close() RPC FIRST so the
        // ACPI→destroy lifecycle (with virsh timeout + orphan reap) runs
        // before xdg_toplevel.close goes to virt-viewer. The RPC handler
        // tears down virt-viewer and the libvirt domain; we still call
        // xdg_toplevel.close afterward as a belt-and-braces so the
        // window disappears even when the control process is missing
        // (degraded image without dbus-python).
        if (typeof window === "object" && window !== null
                && typeof window.secctxAppId === "string"
                && window.secctxAppId.startsWith("qdistro.tier4.")
                && typeof window.ownerUid === "number") {
            const vm = window.secctxAppId.slice("qdistro.tier4.".length);
            if (vm.length > 0) {
                _dispatchTier4Close(vm, window.ownerUid);
            }
        } else if (h >= 0) {
            // Lookup the row from windows by handle so a bare-handle
            // caller (Taskbar / Workspace pass numeric handles) also
            // benefits from the tier-4 close hook.
            for (let i = 0; i < root.windows.count; i++) {
                const w = root.windows.get(i);
                if (w.handle === h
                        && typeof w.secctxAppId === "string"
                        && w.secctxAppId.startsWith("qdistro.tier4.")) {
                    const vm2 = w.secctxAppId.slice("qdistro.tier4.".length);
                    if (vm2.length > 0) _dispatchTier4Close(vm2, w.ownerUid);
                    break;
                }
            }
        }
        qdwinBinding.closeWindow(h);
    }

    // Fire-and-forget Close() RPC on org.qdistro.Tier4VM.Control.uid<N>.
    // The control process owns this name (see qdistro/tier4-vm/
    // tier4_control.py); the same-uid bus + same-uid attestation in the
    // handler defends against cross-uid abuse. We use busctl rather
    // than DBusBinding because the result is a fire-and-forget side
    // effect — the user-visible close is achieved by virt-viewer's
    // exit, the RPC just guarantees the qemu domain doesn't survive.
    function _dispatchTier4Close(vmName, ownerUid) {
        if (!vmName || typeof ownerUid !== "number" || ownerUid < 0) return;
        const busName = "org.qdistro.Tier4VM.Control.uid" + ownerUid;
        Logger.i("Qdwin", "tier4 close vm=" + vmName
                          + " uid=" + ownerUid + " bus=" + busName);
        // The RPC takes no args, returns (bsui). Discard the reply —
        // even denial / failure should fall through to xdg_toplevel.close
        // so the user gets a window dismissal regardless.
        Quickshell.execDetached([
            "busctl", "--user", "--no-pager",
            "call", busName, "/org/qdistro/Tier4VM",
            "org.qdistro.Tier4VM.Control", "Close"
        ]);
    }

    function requestMaximize(window, maximized) {
        const h = _handleOf(window);
        if (h < 0) return;
        qdwinBinding.requestMaximize(h, !!maximized);
    }

    function requestMinimize(window) {
        const h = _handleOf(window);
        if (h < 0) return;
        qdwinBinding.requestMinimize(h);
    }

    // Presentation-only escape hatch for a well-formed remote proxy whose
    // broker attribution never succeeds. This deliberately cannot request
    // source close; RemoteMachineWindows is the sole policy caller.
    function dismissUnattributedRemote(window) {
        const h = _handleOf(window);
        if (h < 0) return;
        qdwinBinding.requestMinimize(h);
    }

    // ── v25 window-manager policy + shortcut helpers ────────────────
    // Push the live WM policy snapshot to the compositor. Called by
    // WindowManagerService whenever the policy changes or the shell
    // (re)binds at >= v25. focusPolicy: 0=click, 1=follow-mouse;
    // placement: 0=center, 1=under-mouse, 2=smart, 3=cascade.
    function applyWmPolicy(focusPolicy, ffmDelayMs, raiseOnClick, raiseOnHover,
                           placement, snapEnabled, snapDistance) {
        if (!qdwinBinding) return;
        qdwinBinding.setWmPolicy(focusPolicy, ffmDelayMs, raiseOnClick,
                                 raiseOnHover, placement, snapEnabled,
                                 snapDistance);
    }
    // ── v28 live input config ───────────────────────────────────────
    // Push the libinput pointer/touchpad snapshot. accelSpeed in milli-units
    // (-1000..1000); accelProfile 0=adaptive/1=flat; scrollMethod 0=none,
    // 1=two-finger, 2=edge, 3=on-button-down. Called by PointerInputService
    // when CapabilityService.pointerConfig is live.
    function applyPointerConfig(accelSpeed, accelProfile, naturalScroll,
                                tapToClick, leftHanded, middleEmulation,
                                disableWhileTyping, scrollMethod) {
      if (!qdwinBinding) return;
      qdwinBinding.setPointerConfig(accelSpeed, accelProfile, naturalScroll,
                                    tapToClick, leftHanded, middleEmulation,
                                    disableWhileTyping, scrollMethod);
    }
    // Push the xkb key-repeat rate (Hz, 0=off) and initial delay (ms). Called
    // by KeyboardInputService when CapabilityService.xkbRepeat is live.
    function applyKeyRepeat(rate, delay) {
      if (!qdwinBinding) return;
      qdwinBinding.setKeyRepeat(rate, delay);
    }
    // WM-shortcut hotkey (de)registration. id is shell-assigned; modifiers is
    // the ctrl=1/alt=2/super=4/shift=8 bitmask; key is a linux input keycode.
    function registerHotkey(id, modifiers, key) {
        if (!qdwinBinding) return;
        qdwinBinding.registerHotkey(id, modifiers, key);
    }
    function unregisterHotkey(id) {
        if (!qdwinBinding) return;
        qdwinBinding.unregisterHotkey(id);
    }
    // Handle-based window actions for the WM shortcuts (which act on the
    // focusedHandle, not a window row object). `tileEdge`: 0=none, 1=left,
    // 2=right. windowState returns the QDWIN_TS_* bitmask (0 if unknown).
    function closeHandle(handle) {
        if (!qdwinBinding || handle <= 0) return;
        // Route through closeWindow(window) so a tier-4 VM window still gets
        // the _dispatchTier4Close() domain-teardown hook (closing the
        // wl_toplevel alone leaves the qemu domain running). Fall back to a
        // raw close only if the handle isn't in our tracked window list.
        const row = _windowByHandle(handle);
        if (row)
            closeWindow(row);
        else
            qdwinBinding.closeWindow(handle);
    }
    function requestMaximizeHandle(handle, maximized) {
        if (!qdwinBinding || handle <= 0) return;
        qdwinBinding.requestMaximize(handle, !!maximized);
    }
    function requestFullscreenHandle(handle, fullscreen) {
        if (!qdwinBinding || handle <= 0) return;
        qdwinBinding.requestFullscreen(handle, !!fullscreen);
    }
    function requestTileHandle(handle, tileEdge) {
        if (!qdwinBinding || handle <= 0) return;
        qdwinBinding.requestTile(handle, tileEdge);
    }
    function requestSetPositionHandle(handle, x, y) {
        if (!qdwinBinding || handle <= 0) return;
        qdwinBinding.requestSetPosition(handle, x, y);
    }
    function setRemoteOutputInput(slotName, enabled) {
        if (!qdwinBinding || qdwinBinding.shellVersion < 32) return false;
        qdwinBinding.setRemoteOutputInput(slotName, !!enabled);
        return true;
    }
    function drainRemoteOutputState(slotName) {
        if (!qdwinBinding || qdwinBinding.shellVersion < 33) return false;
        qdwinBinding.drainRemoteOutputState(slotName);
        return true;
    }
    function windowState(handle) {
        const row = _windowByHandle(handle);
        return row ? (row.state >>> 0) : 0;
    }

    // ── v26 idle / DPMS helpers ─────────────────────────────────────
    // Arm (timeoutMs > 0) or cancel (0) an ext-idle-notify notification for
    // `slot`; idleStateChanged(slot, idle) fires on idled/resumed. PowerService
    // uses slot 0 = inactivity action, slot 1 = display-off.
    function setIdleNotification(slot, timeoutMs) {
        if (!qdwinBinding) return;
        qdwinBinding.setIdleNotification(slot, timeoutMs);
    }
    // Force all outputs on/off (DPMS) via set_display_power (>= v26).
    function setDisplayPower(on) {
        if (!qdwinBinding) return;
        qdwinBinding.setDisplayPower(!!on);
    }

    function cycleKeyboardLayout() { /* qdwin: not in qdwin_shell_v1 */ }

    // -- spawning + session control (compositor-agnostic) -- //

    function spawn(command) {
        Quickshell.execDetached(["sh", "-lc", command]);
    }

    function lock() {
        Quickshell.execDetached(["loginctl", "lock-session"]);
    }

    function logout() {
        Quickshell.execDetached(["loginctl", "terminate-session", "self"]);
    }

    function suspend() {
        Quickshell.execDetached(["systemctl", "suspend"]);
    }

    function hibernate() {
        Quickshell.execDetached(["systemctl", "hibernate"]);
    }

    function lockAndSuspend() {
        lock();
        Qt.callLater(suspend);
    }

    function reboot() {
        Quickshell.execDetached(["systemctl", "reboot"]);
    }

    function rebootToUefi() {
        Quickshell.execDetached(["systemctl", "reboot", "--firmware-setup"]);
    }

    function shutdown() {
        Quickshell.execDetached(["systemctl", "poweroff"]);
    }
}
