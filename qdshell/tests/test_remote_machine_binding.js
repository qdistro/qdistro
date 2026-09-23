// Source guard for the R2 broker-vouched trust-chrome boundary.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const qml = fs.readFileSync(
  path.join(__dirname, "..", "Services/Qdwin/RemoteMachineWindows.qml"), "utf8");

// Ensures: observing a self-shaped secctx app_id does not immediately earn
// trusted colour; broker confirmation is parsed before authorization maps fill.
assert.ok(qml.includes('root._paintBorder(row.handle, "", "", false)'),
  "new remote-looking windows must begin with neutral chrome");
assert.ok(qml.includes('"BindHandleIdentity", "sssst"'),
  "qdshell must request the broker-vouched bound identity");
assert.ok(qml.includes('RM.parseBindIdentity(String(_bindStdout.text || ""))'),
  "BindHandleIdentity output must pass fail-closed parsing");
assert.ok(qml.includes('root._originByHandle[req.handle] = identity.origin'),
  "close authority must come from the accepted broker identity");
assert.ok(qml.includes('root._trustDomainByHandle[req.handle] = identity.trust_domain_id'),
  "trust chrome must use the broker-vouched trust domain");
assert.ok(qml.includes('root._secctxByHandle[req.handle] = req.secctxAppId'),
  "handle authorization must bind the exact secctx observation");
assert.ok(qml.includes('target: "multimachine"'),
  "authorized remote windows must expose the stable operator IPC surface");
assert.ok(qml.includes('if (!root._authorizedHandle(handle)) return false;'),
  "neutral/unpaired lookalikes must not gain IPC focus or close authority");
assert.ok(qml.includes('Qdwin.closeWindow(handle);'),
  "IPC close must enter qdshell's source-mediated remote close path");
for (const operation of ["minimize", "maximize", "restore", "move"])
  assert.ok(qml.includes(`function ${operation}(`),
    `authorized remote windows must expose source-mediated ${operation}`);
assert.ok(qml.includes('"RequestShellOperation", "tsii"'),
  "R3 shell operations must route through the broker");
assert.ok(qml.includes('Qdwin.dismissUnattributedRemote(handle);'),
  "unattributed remote close must retain a local presentation-dismiss action");
assert.ok(qml.includes('delete root._bindAttemptByHandle[req.handle]'),
  "failed binds must clear the one-shot marker before bounded retry");
assert.ok(qml.includes('root._scheduleBindRetry(req)'),
  "transient broker bind failures must be retried");
assert.ok(qml.includes('id: _bindTimeout') && qml.includes('interval: 5000'),
  "serialized broker binds must have a finite timeout");
assert.ok(qml.includes('if (count > 5)'),
  "broker bind retry must be bounded");
assert.ok(qml.includes('root._queueBrokerRequest(['),
  "close and shell operations must remain children of the pinned qdshell peer");
assert.ok(!qml.includes('Quickshell.execDetached(['),
  "broker calls must not detach and lose their pinned qdshell parent");
assert.ok(!qml.includes('Qdwin.requestMaximize(handle'),
  "remote maximize/restore must not mutate the viewer-local proxy");
assert.ok(!qml.includes('Qdwin.requestMinimize(handle)'),
  "authorized remote minimize must not mutate the viewer-local proxy");
assert.ok(qml.includes('"[mm] neutral chrome handle="'),
  "live evidence must expose the neutral-before-bind boundary");
assert.ok(qml.includes('const nested = w.remoteNestedAuthorized === true'),
  "nested remote attribution must require qdwin's protected identity sidecar");
assert.ok(qml.includes('root._secctxByHandle[w.handle] = identityKey'),
  "protected nested identity must bind authorization to its exact stream");
assert.ok(qml.includes('&& row.transport === "rdp"'),
  "protected nested identities must not be sent to the legacy RDP broker bind");

// Ensures: the old unchecked fire-and-forget bind cannot silently return.
assert.ok(!qml.includes('"BindHandle", "sssst"'),
  "unchecked legacy BindHandle call must not return");
assert.ok(!qml.includes('RM.colourForOrigin(row.origin)'),
  "secctx-parsed origin alone must not select trusted chrome");

console.log("remote-machine-binding: broker-vouched chrome invariants passed");
