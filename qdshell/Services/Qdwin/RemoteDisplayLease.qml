pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.Qdwin
import "OutputLayout.js" as OutputLayout
import "RemoteDisplayLease.js" as Lease

// Shell-side executor for controller-verified remote-display slot mutations.
// ClaimLayout/AcknowledgeLayout authenticate this process (or its direct
// busctl child) at the controller service. No same-uid command target exists.
Singleton {
    id: root

    readonly property string bus: "org.qdistro.MultiMachineDisplay1"
    readonly property string path: "/org/qdistro/MultiMachineDisplay1"
    readonly property string iface: "org.qdistro.MultiMachineDisplay1"
    property var _pending: null
    property bool _serviceSeen: false
    property bool _claimBusy: false
    property bool _ackBusy: false
    property var _inputPending: null
    property bool _inputClaimBusy: false
    property bool _inputAckBusy: false
    property bool _inputDrainPending: false

    function _claimCommand() {
        return ["busctl", "--user", "--timeout=2s", "call",
                bus, path, iface, "ClaimLayout"];
    }

    function _ack(result) {
        if (!_pending || _ackBusy) return;
        _ackBusy = true;
        _ackProc.command = [
            "busctl", "--user", "--timeout=2s", "call",
            bus, path, iface, "AcknowledgeLayout", "sts",
            _pending.request_id, String(_pending.generation), result
        ];
        _ackProc.running = true;
    }

    function _inputClaimCommand() {
        return ["busctl", "--user", "--timeout=2s", "call",
                bus, path, iface, "ClaimInput"];
    }

    function _inputAck(result) {
        if (!_inputPending || _inputAckBusy) return;
        _inputAckBusy = true;
        _inputAckProc.command = [
            "busctl", "--user", "--timeout=2s", "call",
            bus, path, iface, "AcknowledgeInput", "sts",
            _inputPending.request_id, String(_inputPending.generation), result
        ];
        _inputAckProc.running = true;
    }

    function _applyInput(request) {
        _inputPending = request;
        if (!Qdwin.setRemoteOutputInput(
                request.slot_name, request.enabled)) {
            _inputAck("failed");
        }
    }

    function _apply(request) {
        const live = OutputLayout.layoutFromSnapshots(Qdwin.outputs || []);
        const layout = Lease.buildSlotLayout(live, request);
        if (!layout) {
            Logger.w("RemoteDisplayLease", "invalid/unavailable slot request");
            _pending = request;
            _ack("failed");
            return;
        }
        const modes = {};
        for (let i = 0; i < (Qdwin.outputs || []).length; i++) {
            const output = Qdwin.outputs[i];
            modes[output.name] = output.modes || [];
        }
        const verdict = OutputLayout.validateLayout(layout, modes);
        _pending = request;
        if (!verdict.ok || !Qdwin.applyOutputLayoutTagged(
                layout, Qdwin.outputSerial, request.request_id)) {
            Logger.w("RemoteDisplayLease",
                "slot apply rejected before qdwin: " + verdict.errors.join(",")
                + "; requested=" + request.width + "x" + request.height
                + "; advertised="
                + JSON.stringify(modes[request.slot_name] || []));
            _ack("failed");
        }
    }

    Timer {
        interval: 250
        repeat: true
        running: root._serviceSeen
        onTriggered: {
            if (root._claimBusy || root._pending || root._ackBusy
                    || root._inputPending || root._inputClaimBusy
                    || root._inputAckBusy || root._inputDrainPending) return;
            root._claimBusy = true;
            _claimProc.command = root._claimCommand();
            _claimProc.running = true;
        }
    }

    Timer {
        interval: 100
        repeat: true
        running: root._serviceSeen
        onTriggered: {
            if (root._claimBusy || root._pending || root._ackBusy
                    || root._inputPending || root._inputClaimBusy
                    || root._inputAckBusy || root._inputDrainPending) return;
            root._inputClaimBusy = true;
            _inputClaimProc.command = root._inputClaimCommand();
            _inputClaimProc.running = true;
        }
    }

    // A lost compositor result must not wedge the controller mailbox. The
    // request has already expired at this point, so clear it without an
    // acknowledgement; the controller will fail the transaction and retain
    // the input-disabled safe state.
    Timer {
        interval: 250
        repeat: true
        running: root._inputPending !== null && !root._inputAckBusy
        onTriggered: {
            if (Math.floor(Date.now() / 1000)
                    < Number(root._inputPending.expires_at)) return;
            Logger.w("RemoteDisplayLease",
                "input transaction expired before compositor result");
            root._inputPending = null;
            root._inputDrainPending = false;
        }
    }

    // One sleeping process while undocked, rather than periodic process
    // creation. It exits successfully when the controller owns its bus name;
    // a later failed ClaimLayout re-arms the waiter.
    Process {
        id: _serviceWaitProc
        command: ["gdbus", "wait", "--session", root.bus]
        running: !root._serviceSeen
        onExited: (exitCode, exitStatus) => {
            if (exitCode === 0)
                root._serviceSeen = true;
        }
    }

    Process {
        id: _claimProc
        running: false
        stdout: StdioCollector { id: _claimStdout }
        stderr: StdioCollector { id: _claimStderr }
        onExited: (exitCode, exitStatus) => {
            root._claimBusy = false;
            if (exitCode !== 0) {
                root._serviceSeen = false;
                return;
            }
            const request = Lease.parseBusctlString(_claimStdout.text || "");
            if (request) root._apply(request);
        }
    }

    Process {
        id: _ackProc
        running: false
        stderr: StdioCollector { id: _ackStderr }
        onExited: (exitCode, exitStatus) => {
            if (exitCode !== 0)
                Logger.w("RemoteDisplayLease",
                    "layout acknowledgement failed: "
                    + String(_ackStderr.text || "").trim());
            root._ackBusy = false;
            root._pending = null;
        }
    }


    Process {
        id: _inputClaimProc
        running: false
        stdout: StdioCollector { id: _inputClaimStdout }
        onExited: (exitCode, exitStatus) => {
            root._inputClaimBusy = false;
            if (exitCode !== 0) {
                root._serviceSeen = false;
                return;
            }
            const request = Lease.parseBusctlInput(
                _inputClaimStdout.text || "");
            if (request) root._applyInput(request);
        }
    }

    Process {
        id: _inputAckProc
        running: false
        stderr: StdioCollector { id: _inputAckStderr }
        onExited: (exitCode, exitStatus) => {
            if (exitCode !== 0)
                Logger.w("RemoteDisplayLease",
                    "input acknowledgement failed: "
                    + String(_inputAckStderr.text || "").trim());
            root._inputAckBusy = false;
            root._inputPending = null;
            root._inputDrainPending = false;
        }
    }

    Connections {
        target: Qdwin
        function onOutputLayoutTaggedResult(tag, ok, cancelled) {
            if (!root._pending || tag !== root._pending.request_id) return;
            root._ack(ok ? "applied" : (cancelled ? "cancelled" : "failed"));
        }
        function onRemoteOutputInputResult(outputName, enabled, applied) {
            if (!root._inputPending
                    || outputName !== root._inputPending.slot_name
                    || enabled !== root._inputPending.enabled) return;
            if (!applied || enabled) {
                root._inputAck(applied ? "applied" : "failed");
                return;
            }
            root._inputDrainPending = true;
            if (!Qdwin.drainRemoteOutputState(outputName)) {
                root._inputDrainPending = false;
                root._inputAck("failed");
            }
        }
        function onRemoteOutputDrainResult(outputName, applied) {
            if (!root._inputPending || !root._inputDrainPending
                    || outputName !== root._inputPending.slot_name) return;
            root._inputDrainPending = false;
            root._inputAck(applied ? "applied" : "failed");
        }
    }
}
