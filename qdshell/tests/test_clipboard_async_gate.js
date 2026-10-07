// Asynchronous broker checks in ClipboardGate.qml: the set-time and
// receive-time gates start a binding request and finish the decision from
// _onClipboardCheckFinished. Runs the real QML functions against a fake
// binding (same extraction as test_gate_process_queue.js).
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const ClipboardBroker = require('../Services/Qdshell/ClipboardBroker.js');
const ClipboardSilo = require('../Services/Qdshell/ClipboardSilo.js');
const ClipboardDenyCoalesce = require('../Services/Qdshell/ClipboardDenyCoalesce.js');
const ClipboardFocusClear = require('../Services/Qdshell/ClipboardFocusClear.js');

const SRC = 11, DST = 22, NONE = 4294967295;

function gate({async = true} = {}) {
    const source = fs.readFileSync(
        path.join(__dirname, '../Services/Qdshell/ClipboardGate.qml'), 'utf8');
    const log = [], clears = [], answers = [], started = [], syncCalls = [];
    let nextId = 0;
    const binding = {
        focusedHandle: DST,
        shellVersion: 35,
        clearSelection(seat, isPrimary) { clears.push({seat, isPrimary}); },
        sendDataOfferReceiveDecision(handle, allow) { answers.push({handle, allow}); },
        startCheckClipboardTransfer(...args) { started.push({kind: 'transfer', args}); return ++nextId; },
        startCheckClipboardReceive(...args) { started.push({kind: 'receive', args}); return ++nextId; },
        checkClipboardTransfer(...args) { syncCalls.push(args); return {exitCode: 0, stdout: 's "allow"\n'}; },
        checkClipboardReceive(...args) { syncCalls.push(args); return {exitCode: 0, stdout: 's "allow"\n'}; },
    };
    const line = (...parts) => log.push(parts.join(' '));
    const ctx = vm.createContext({
        ClipboardBroker, ClipboardSilo, ClipboardDenyCoalesce, ClipboardFocusClear, Date,
        Logger: {i: line, d: line, w: line, e: line},
        Qt: {callLater() {}},
        _verifyProc: {set command(v) {}, set running(v) {}},
        _binding: binding,
        _asyncChecks: async,
        _receiveGateShellVersion: 15,
        _pendingChecks: {},
        _selectionGen: {'0': 0, '1': 0},
        _handleToSilo: {[SRC]: 'qdistro:alpha', [DST]: 'qdistro:beta'},
        _handleToAppId: {[SRC]: 'alpha', [DST]: 'beta'},
        _handleToSandboxEngine: {[SRC]: 'qdistro', [DST]: 'qdistro'},
        _handleToIdentity: {},
        _verifyCache: {}, _verifyInFlight: {}, _verifyQueue: [], _verifyActive: null,
        _verifyGeneration: 0,
        _pendingSrcIdentity: null, _pendingSrcPeer: null,
        _selectionSourceSilo: {}, _lastDenyClearByKey: {}, _denyClearCoalesceMs: 500,
        _tier4AllowedMimeBases: ['text/plain', 'text/uri-list'],
    });
    ctx.root = ctx;
    for (const m of source.matchAll(/^ {4}function \w+\([^]*?^ {4}}/gm))
        vm.runInContext(m[0], ctx);
    const gateLines = () => log.filter(l => l.includes('CLIPBOARD_GATE ') || l.includes('CLIPBOARD_RECEIVE_GATE '));
    return {c: ctx, binding, log, gateLines, clears, answers, started, syncCalls};
}

// ensures: a set-time check does not decide until the broker reports, then a
// deny clears the selection exactly once.
{
    const g = gate();
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    assert.strictEqual(g.started.length, 1);
    assert.strictEqual(g.started[0].kind, 'transfer');
    assert.strictEqual(g.gateLines().length, 0, 'no verdict before the broker answers');
    assert.strictEqual(g.clears.length, 0);
    g.c._onClipboardCheckFinished(1, 0, 's "deny"\n', false);
    assert.strictEqual(g.clears.length, 1);
    assert.match(g.gateLines()[0], /verdict=deny reason=broker:deny/);
    // ensures: a duplicate report for the same id is ignored.
    g.c._onClipboardCheckFinished(1, 0, 's "deny"\n', false);
    assert.strictEqual(g.clears.length, 1);
    assert.strictEqual(g.gateLines().length, 1);
}

// ensures: an allow does not clear; a timeout or start failure denies and clears.
{
    const g = gate();
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    g.c._onClipboardCheckFinished(1, 0, 's "allow"\n', false);
    assert.strictEqual(g.clears.length, 0);
    assert.match(g.gateLines()[0], /verdict=allow reason=broker:allow/);
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    g.c._onClipboardCheckFinished(2, -1, '', true);
    assert.strictEqual(g.clears.length, 1);
    assert.match(g.gateLines()[1], /verdict=deny reason=broker-unavailable/);
}

// ensures: a stale deny for a replaced selection is logged but cannot clear the
// newer selection; the newer selection's own deny still clears.
{
    const g = gate();
    g.c._onSelectionSet('default', SRC, 'text/plain', false);   // id 1
    g.c._onSelectionSet('default', SRC, 'text/plain', false);   // id 2 replaces it
    g.c._onClipboardCheckFinished(1, 0, 's "deny"\n', false);
    assert.strictEqual(g.clears.length, 0, 'superseded deny must not clear');
    assert.match(g.gateLines()[0], /verdict=deny/);
    assert.ok(g.log.some(l => l.includes('CLIPBOARD_GATE_SUPERSEDED')));
    g.c._onClipboardCheckFinished(2, 0, 's "deny"\n', false);
    assert.strictEqual(g.clears.length, 1);
    // ensures: the regular and primary selections have separate generations.
    g.c._onSelectionSet('default', SRC, 'text/plain', true);    // id 3, primary
    g.c._onSelectionSet('default', SRC, 'text/plain', false);   // id 4, regular
    g.c._onClipboardCheckFinished(3, 0, 's "deny"\n', false);
    assert.strictEqual(g.clears.length, 2);
    assert.strictEqual(g.clears[1].isPrimary, true);
}

// ensures: local denies (unknown identity) still decide at once, without a broker call.
{
    const g = gate();
    g.binding.focusedHandle = NONE;
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    assert.strictEqual(g.started.length, 0);
    assert.match(g.gateLines()[0], /verdict=deny reason=unknown-identity/);
    assert.strictEqual(g.clears.length, 1);
}

// ensures: a receive is answered exactly once, from the broker report; unknown
// identity is answered at once without a broker call.
{
    const g = gate();
    g.c._onDataOfferReceivePending(77, 'default', SRC, DST, 'text/plain');
    assert.strictEqual(g.started.length, 1);
    assert.strictEqual(g.started[0].kind, 'receive');
    assert.strictEqual(g.answers.length, 0, 'no answer before the broker reports');
    g.c._onClipboardCheckFinished(1, 0, 's "allow"\n', false);
    assert.deepStrictEqual(g.answers, [{handle: 77, allow: true}]);
    g.c._onClipboardCheckFinished(1, 0, 's "deny"\n', false);
    assert.strictEqual(g.answers.length, 1);
    g.c._onDataOfferReceivePending(78, 'default', SRC, DST, 'text/plain');
    g.c._onClipboardCheckFinished(2, -1, '', true);
    assert.deepStrictEqual(g.answers[1], {handle: 78, allow: false});
    assert.match(g.gateLines().pop(), /CLIPBOARD_RECEIVE_GATE .*verdict=deny reason=broker-unavailable/);
    g.c._onDataOfferReceivePending(79, 'default', SRC, NONE, 'text/plain');
    assert.strictEqual(g.started.length, 2);
    assert.deepStrictEqual(g.answers[2], {handle: 79, allow: false});
}

// ensures: replies for a lost connection's requests are ignored.
{
    const g = gate();
    g.c._onDataOfferReceivePending(80, 'default', SRC, DST, 'text/plain');
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    g.c._onBindingLost();
    g.c._onClipboardCheckFinished(1, 0, 's "allow"\n', false);
    g.c._onClipboardCheckFinished(2, 0, 's "deny"\n', false);
    assert.strictEqual(g.answers.length, 0);
    assert.strictEqual(g.clears.length, 0);
}

// ensures: every answered request leaves no pending entry behind.
{
    const g = gate();
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    g.c._onDataOfferReceivePending(82, 'default', SRC, DST, 'text/plain');
    assert.strictEqual(Object.keys(g.c._pendingChecks).length, 2);
    g.c._onClipboardCheckFinished(2, -1, '', false);
    g.c._onClipboardCheckFinished(1, -1, '', true);
    assert.strictEqual(Object.keys(g.c._pendingChecks).length, 0);
    assert.deepStrictEqual(g.answers, [{handle: 82, allow: false}]);
    assert.strictEqual(g.clears.length, 1);
}

// ensures: a compositor without the receive gate (shell < v15) never leaves
// a selection live on a pending verdict: set time decides synchronously,
// also after a rebind drops the capability, and resumes async once it is back.
{
    const g = gate();
    g.binding.shellVersion = 14;
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    assert.strictEqual(g.started.length, 0);
    assert.strictEqual(g.syncCalls.length, 1);
    assert.strictEqual(g.gateLines().length, 1);
    g.binding.shellVersion = 35;
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    assert.strictEqual(g.started.length, 1);
    g.c._onBindingLost();   // rebind to an older compositor
    g.c._handleToSilo = {[SRC]: 'qdistro:alpha', [DST]: 'qdistro:beta'};
    g.c._handleToAppId = {[SRC]: 'alpha', [DST]: 'beta'};
    g.c._handleToSandboxEngine = {[SRC]: 'qdistro', [DST]: 'qdistro'};
    g.binding.shellVersion = 14;
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    assert.strictEqual(g.started.length, 1);
    assert.strictEqual(g.syncCalls.length, 2);
    g.binding.shellVersion = undefined;
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    assert.strictEqual(g.started.length, 1);
    assert.strictEqual(g.syncCalls.length, 3);
}

// ensures: without the async API the synchronous calls decide in place.
{
    const g = gate({async: false});
    g.c._onSelectionSet('default', SRC, 'text/plain', false);
    g.c._onDataOfferReceivePending(81, 'default', SRC, DST, 'text/plain');
    assert.strictEqual(g.started.length, 0);
    assert.strictEqual(g.syncCalls.length, 2);
    assert.deepStrictEqual(g.answers, [{handle: 81, allow: true}]);
    assert.strictEqual(g.gateLines().length, 2);
}

// ensures: identity verification waits 2 s for the broker, not 200 ms.
{
    const source = fs.readFileSync(
        path.join(__dirname, '../Services/Qdshell/ClipboardGate.qml'), 'utf8');
    assert.ok(source.includes('"--timeout=2s", "call", "org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1", "org.qdistro.AdminBroker1", "VerifyClientIdentity"'));
    assert.ok(!source.includes('--timeout=200ms'));
}

console.log('clipboard async gate: ok');
