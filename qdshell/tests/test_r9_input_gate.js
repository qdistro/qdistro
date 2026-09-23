// Source guard for the authenticated R9 compositor input gate.
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const Lease = require("../Services/Qdwin/RemoteDisplayLease.js");

const root = path.resolve(__dirname, "..");
const header = fs.readFileSync(
  path.join(root, "qml-plugin/qdwin-binding.h"), "utf8");
const source = fs.readFileSync(
  path.join(root, "qml-plugin/qdwin-binding.cpp"), "utf8");
const qdwin = fs.readFileSync(
  path.join(root, "Services/Qdwin/Qdwin.qml"), "utf8");
const executor = fs.readFileSync(
  path.join(root, "Services/Qdwin/RemoteDisplayLease.qml"), "utf8");

assert.ok(header.includes("setRemoteOutputInput(const QString &outputName"));
assert.ok(source.includes("constexpr uint32_t kBindVersion = 34;"));
assert.ok(source.includes("qdwin_shell_v1_set_remote_output_input("));
assert.ok(source.includes("shellVersion_ < 34"));
assert.ok(source.includes("remote_output_input_result"));
assert.ok(source.includes("qdwin_shell_v1_drain_remote_output_state("));
assert.ok(source.includes("remote_output_drain_result"));
assert.ok(qdwin.includes("signal remoteOutputInputResult("));
assert.ok(!qdwin.includes("function setRemoteInput(handle"),
  "input gate must not be added to general qdwin IPC");
assert.ok(executor.includes('"ClaimInput"'));
assert.ok(executor.includes('"AcknowledgeInput"'));
assert.ok(executor.includes("Qdwin.setRemoteOutputInput("));
assert.ok(executor.includes("onRemoteOutputInputResult"));
assert.ok(executor.includes("onRemoteOutputDrainResult"));
assert.ok(executor.includes("Qdwin.drainRemoteOutputState("));
assert.ok(executor.includes("input transaction expired before compositor result"));
assert.ok(executor.includes("root._inputDrainPending = false"));

const now = Math.floor(Date.now() / 1000);
const request = {
  schema: "qdistro-mm-shell-input-v1",
  request_id: "a".repeat(32),
  generation: 90,
  session_id: "dock-90",
  slot_name: "rdp-0",
  enabled: true,
  expires_at: now + 10,
};
assert.strictEqual(Lease.validateInputRequest(request, now), true);
assert.strictEqual(Lease.validateInputRequest({...request, slot_name: "headless"}, now), false);
assert.strictEqual(Lease.validateInputRequest({...request, expires_at: now}, now), false);
assert.strictEqual(Lease.validateInputRequest({...request, extra: 1}, now), false);

console.log("r9-input-gate: authenticated qdshell compositor gate passed");
