// Source invariants for the qdwin-binding shell-capture client.
//
// Pins the startup-ordering contract: connectAndBind() performs synchronous
// wl_display roundtrips (hello + qdwin bind replay are dispatched inside it),
// so the constructor must NEVER call it directly — QML attaches
// onBoundChanged / protocol handlers only after construction, and a
// synchronous connect would emit boundChanged into the void, skipping
// ClipboardGate / WM-policy / replay initialization. The initial connect
// must be deferred to the live event loop (queued invocation).
//
// Also pins the capture verb's authority posture: root-only SO_PEERCRED in
// the ctrl server, and the exact designated-output check in captureOutput.
const assert = require("assert");
const fs = require("fs");
const path = require("path");

const binding = fs.readFileSync(
    path.join(__dirname, "..", "qml-plugin", "qdwin-binding.cpp"), "utf8");
const ctrl = fs.readFileSync(
    path.join(__dirname, "..", "qml-plugin", "ctrl-server.cpp"), "utf8");

// --- startup ordering ------------------------------------------------------
const ctorMatch = binding.match(
    /QdwinBinding::QdwinBinding\(QObject \*parent\)[^{]*\{([\s\S]*?)\n\}/);
assert.ok(ctorMatch, "QdwinBinding constructor not found");
const ctorBody = ctorMatch[1];
// The only connectAndBind() in the constructor must sit inside a
// Qt::QueuedConnection invokeMethod lambda (deferred to the event loop).
assert.ok(
    /QMetaObject::invokeMethod\([\s\S]*?connectAndBind\(\);[\s\S]*?Qt::QueuedConnection\)/
        .test(ctorBody),
    "constructor must defer the initial connectAndBind() via a queued invocation");
// Outside the queued invocation and the reconnect-timer lambda (both run
// from the live event loop), no direct call may remain.
const direct = ctorBody
    .replace(/\/\/[^\n]*/g, "")
    .replace(/QMetaObject::invokeMethod\([\s\S]*?Qt::QueuedConnection\);/, "")
    .replace(/connect\(&reconnectTimer_[\s\S]*?\}\);/, "")
    .includes("connectAndBind()");
assert.ok(!direct,
    "constructor must not also call connectAndBind() synchronously");

// --- capture authority posture --------------------------------------------
assert.ok(ctrl.includes("SO_PEERCRED"),
    "ctrl server must authenticate the capture peer via SO_PEERCRED");
assert.ok(/cred\.uid != 0|uid != 0|!= 0/.test(ctrl) &&
          ctrl.includes("capture requires authenticated root peer"),
    "capture verb must be root-peer-only with the documented refusal message");
assert.ok(
    binding.includes('outputName != QStringLiteral("Virtual-1")'),
    "captureOutput must refuse every output except the designated Virtual-1");
assert.ok(
    binding.includes("refusing capture of non-designated output"),
    "non-designated output refusal message must be stable (smoke greps it)");

console.log("PASS: capture binding startup deferral + authority invariants");
