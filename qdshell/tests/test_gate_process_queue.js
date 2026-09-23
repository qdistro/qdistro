// Exercise the actual QML functions with one-child-at-a-time Process semantics.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const BrokerGate = require('../Services/Qdshell/BrokerGate.js');

function harness(file, indent, state, processName) {
    const source = fs.readFileSync(path.join(__dirname, '../Services/Qdshell', file), 'utf8');
    const later = [], launches = [];
    let command = [], running = false;
    const proc = {
        set command(value) {
            assert.strictEqual(running, false, 'active Process command must be immutable');
            command = value;
        },
        set running(value) {
            if (value) {
                assert.strictEqual(running, false, 'only one child may run');
                launches.push(command.slice());
            }
            running = value;
        }
    };
    const ctx = vm.createContext(Object.assign(state, {
        BrokerGate, Date, Logger: {w() {}, e() {}},
        Qt: {callLater(fn) { later.push(fn); }}, [processName]: proc
    }));
    ctx.root = ctx;
    const regex = new RegExp('^' + ' '.repeat(indent) + 'function \\w+\\([^]*?^' + ' '.repeat(indent) + '}', 'gm');
    for (const match of source.matchAll(regex)) vm.runInContext(match[0], ctx);
    return {ctx, launches, exit(code, output, finish) {
        running = false;
        ctx[finish](code, output);
        while (later.length) later.shift()();
    }};
}
function identity(pid) {
    return {pid, starttime: 123, uid: 1000, exe: '/app', label: '',
        sandboxEngine: 'qdistro', appId: 'work', instanceId: 'launch'};
}
function clipboard() {
    return harness('ClipboardGate.qml', 4, {
        _handleToIdentity: {11: identity(101), 22: identity(202)},
        _handleToSilo: {}, _handleToAppId: {}, _handleToSandboxEngine: {},
        _verifyCache: {}, _verifyInFlight: {}, _verifyQueue: [], _verifyActive: null,
        _verifyGeneration: 0
    }, '_verifyProc');
}
{
    const h = clipboard(), c = h.ctx;
    const a = c._verifyKey(c._handleToIdentity[11]), b = c._verifyKey(c._handleToIdentity[22]);
    // ensures: A's allow cannot verify B, and concurrent calls actually launch both.
    assert.strictEqual(c._ensureVerified(11), false);
    assert.strictEqual(c._ensureVerified(22), false);
    assert.strictEqual(h.launches.length, 1);
    h.exit(0, 'b true', '_finishVerification');
    assert.strictEqual(c._verifyCache[a].verified, true);
    assert.strictEqual(c._verifyCache[b], undefined);
    assert.strictEqual(h.launches.length, 2);
    assert.ok(h.launches[0].includes('101'));
    assert.ok(h.launches[1].includes('202'));
    h.exit(0, 'b false', '_finishVerification');
    assert.strictEqual(c._verifyCache[b].verified, false);
    assert.strictEqual(Object.keys(c._verifyInFlight).length, 0);
    // ensures: unavailable/denied verification is retried after its bounded lifetime.
    c._verifyCache[b].expires = 0;
    c._ensureVerified(22);
    assert.strictEqual(h.launches.length, 3);
    h.exit(0, 'garbage true', '_finishVerification');
    assert.strictEqual(c._verifyCache[b].verified, false);
    // ensures: a changed security tuple cannot reuse a prior successful attestation.
    c._handleToIdentity[11].appId = 'personal';
    assert.strictEqual(c._ensureVerified(11), false);
    h.exit(0, 'b true', '_finishVerification');
    c._onToplevelRemoved(11);
    assert.strictEqual(c._handleToIdentity[11], undefined);
}
{
    const h = clipboard(), c = h.ctx;
    c._ensureVerified(11);
    c._onToplevelRemoved(11);
    // ensures: a late result for a destroyed client is discarded.
    h.exit(0, 'b true', '_finishVerification');
    assert.strictEqual(Object.keys(c._verifyCache).length, 0);
    c._ensureVerified(22);
    c._onBindingLost();
    h.exit(0, 'b true', '_finishVerification');
    // ensures: compositor generation changes invalidate pending attestations and handle maps.
    assert.strictEqual(Object.keys(c._handleToIdentity).length, 0);
    assert.strictEqual(Object.keys(c._verifyCache).length, 0);
}
{
    const h = harness('HooksGate.qml', 2, {_queue: [], _active: null,
        brokerBus: 'broker', brokerPath: '/broker', brokerIface: 'broker'}, '_checkProcess');
    const c = h.ctx, allowed = [];
    let denied = 0;
    c.gate('startup', 'first', () => allowed.push('first'));
    c.gate('startup', 'second', () => allowed.push('second'));
    // ensures: concurrent hooks keep callbacks attached to the correct command.
    assert.strictEqual(h.launches.length, 1);
    h.exit(0, 's "allow"', '_finishCheck');
    assert.deepStrictEqual(allowed, ['first']);
    assert.strictEqual(h.launches.length, 2);
    h.exit(0, 's "deny"', '_finishCheck');
    assert.deepStrictEqual(allowed, ['first']);
    // ensures: errors, malformed replies and unknown decisions never execute hooks.
    for (const [code, out] of [[1, 's "allow"'], [0, 's "allow" junk'], [0, ''], [0, 's "unknown"'], [0, 's "transform"']]) {
        c.gate('startup', 'forbidden', () => allowed.push('forbidden'), () => denied++);
        h.exit(code, out, '_finishCheck');
        if (c._active) {
            assert.strictEqual(c._active.phase, 'request');
            h.exit(0, 's "allow"', '_finishCheck');
        }
        assert.deepStrictEqual(allowed, ['first']);
    }
    assert.strictEqual(denied, 5, 'optional denied hooks must complete their caller without executing');
    c.gate('startup', 'first', () => c.gate('startup', 'third', () => allowed.push('third')));
    h.exit(0, 's \"allow\"', '_finishCheck');
    h.exit(0, 's \"allow\"', '_finishCheck');
    assert.deepStrictEqual(allowed, ['first', 'third']);
}
console.log('gate Process queues: identity correlation, expiry, stale replies, and fail-closed hooks PASS');
