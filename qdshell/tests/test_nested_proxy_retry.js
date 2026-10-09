// Nested-proxy gate retry: an unsuccessful broker CheckPermission call
// (nonzero exit — spawn stall, call timeout, broker restarting) must DEFER
// the held proxy and re-ask the broker a bounded number of times, not deny
// it outright. One transient call used to destroy a legit disposable's
// proxy (qdwin-taskbar-isolation bats failure, 2026-10-09). Runs the real
// Qdwin.qml functions against a fake binding (same extraction as
// test_clipboard_async_gate.js); the QML Timer and signal handlers are
// pinned by source assertions at the bottom.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const BrokerGate = require('../Services/Qdshell/BrokerGate.js');

const qml = fs.readFileSync(
    path.join(__dirname, '../Services/Qdwin/Qdwin.qml'), 'utf8');

function gate({checkPermission, bound = true} = {}) {
    const decisions = [], log = [], timerCalls = [];
    const timer = {
        running: false,
        restart() { this.running = true; timerCalls.push('restart'); },
        stop() { this.running = false; timerCalls.push('stop'); },
    };
    const binding = { bound };
    if (checkPermission !== null) {
        const script = checkPermission.slice();
        binding.checkPermission = () =>
            script.length > 1 ? script.shift() : script[0];
        binding.nestedProxyDecision = (handle, decision, reason) =>
            decisions.push({handle, decision, reason});
    } else {
        binding.nestedProxyDecision = (handle, decision, reason) =>
            decisions.push({handle, decision, reason});
    }
    const line = (...parts) => log.push(parts.join(' '));
    const ctx = vm.createContext({
        BrokerGate, Date,
        Logger: {i: line, d: line, w: line, e: line},
        qdwinBinding: binding,
        nestedProxyRetryTimer: timer,
        _nestedProxyRetries: {},
        _nestedProxyMaxAttempts: 4,
        _nestedProxyRetryIntervalMs: 2000,
    });
    ctx.root = ctx;
    for (const m of qml.matchAll(/^ {4}function \w+\([^]*?^ {4}}/gm))
        vm.runInContext(m[0], ctx);
    const gateLines = () => log.filter(l => l.includes('NESTED_PROXY_GATE'));
    return {c: ctx, binding, decisions, log, gateLines, timer, timerCalls};
}

const FAIL = {exitCode: 1, stdout: '', stderr: 'timeout', timedOut: true};
const ALLOW = {exitCode: 0, stdout: 's "allow"\n'};
const DENY = {exitCode: 0, stdout: 's "deny"\n'};
const GARBAGE = {exitCode: 0, stdout: 'not busctl output\n'};

// ensures: one unsuccessful broker call defers the proxy (decision=2) and
// schedules a retry instead of destroying it.
{
    const g = gate({checkPermission: [FAIL, ALLOW]});
    g.c._decideNestedProxy(7, 'org.qd.app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 7, decision: 2, reason: 'broker-unavailable'}]);
    assert.strictEqual(g.c._nestedProxyRetries[7].attempt, 1);
    assert.ok(g.timer.running, 'retry timer must be armed after a defer');
    assert.match(g.gateLines()[0],
        /verdict=defer reason=broker-unavailable attempt=1\/4/);
}

// ensures: a broker that answers on retry releases the proxy with allow and
// the retry entry + timer are cleaned up.
{
    const g = gate({checkPermission: [FAIL, ALLOW]});
    g.c._decideNestedProxy(7, 'org.qd.app', 1000);
    // Backdate the due time so the entry is eligible on the tick.
    g.c._nestedProxyRetries[7].due = 0;
    g.c._retryNestedProxyDecisions();
    assert.deepStrictEqual(g.decisions,
        [{handle: 7, decision: 2, reason: 'broker-unavailable'},
         {handle: 7, decision: 0, reason: 'broker:allow'}]);
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
    assert.ok(!g.timer.running, 'retry timer must stop once empty');
    assert.match(g.gateLines().pop(), /verdict=allow reason=broker:allow/);
}

// ensures: the retry budget is bounded — after _nestedProxyMaxAttempts
// unsuccessful calls the proxy is denied, not deferred forever. Exactly
// maxAttempts broker calls happen.
{
    const calls = [];
    const g = gate({checkPermission: [FAIL]});
    const orig = g.binding.checkPermission;
    g.binding.checkPermission = (...a) => { calls.push(a); return FAIL; };
    g.c._decideNestedProxy(9, 'app', 1000);           // attempt 1
    for (let i = 0; i < 3; i++) {                     // attempts 2-4
        if (g.c._nestedProxyRetries[9])
            g.c._nestedProxyRetries[9].due = 0;
        g.c._retryNestedProxyDecisions();
    }
    assert.strictEqual(calls.length, 4, 'exactly maxAttempts broker calls');
    assert.strictEqual(g.decisions.filter(d => d.decision === 2).length, 3);
    assert.deepStrictEqual(g.decisions[g.decisions.length - 1],
        {handle: 9, decision: 1, reason: 'broker-unavailable'});
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
    assert.match(g.gateLines().pop(),
        /verdict=deny reason=broker-unavailable attempt=4\/4/);
}

// ensures: an explicit broker deny stays immediate — no defer, no retry.
{
    const g = gate({checkPermission: [DENY]});
    g.c._decideNestedProxy(3, 'app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 3, decision: 1, reason: 'broker:deny'}]);
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
    assert.ok(!g.timer.running);
}

// ensures: a malformed broker reply (exit 0, unparseable) is terminal —
// a real response we cannot trust is never retried into an allow.
{
    const g = gate({checkPermission: [GARBAGE, ALLOW]});
    g.c._decideNestedProxy(4, 'app', 1000);
    g.c._retryNestedProxyDecisions();
    assert.deepStrictEqual(g.decisions,
        [{handle: 4, decision: 1, reason: 'broker-malformed'}]);
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
}

// ensures: an unknown verdict word from the broker is terminal deny.
{
    const g = gate({checkPermission: [{exitCode: 0, stdout: 's "pending"\n'}]});
    g.c._decideNestedProxy(5, 'app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 5, decision: 1, reason: 'broker:pending'}]);
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
}

// ensures: a missing checkPermission method (permanent condition) is a
// terminal deny, never deferred.
{
    const g = gate({checkPermission: null});
    g.c._decideNestedProxy(6, 'app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 6, decision: 1, reason: 'broker-unavailable'}]);
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
    assert.ok(!g.timer.running);
}

// ensures: retries are scoped to the compositor connection — an unbound
// binding drops the whole map and stops the timer (handles may be reused
// by a restarted compositor for different proxies).
{
    const g = gate({checkPermission: [FAIL, ALLOW]});
    g.c._decideNestedProxy(8, 'app', 1000);
    assert.ok(g.c._nestedProxyRetries[8], 'entry pending');
    g.binding.bound = false;
    g.c._retryNestedProxyDecisions();
    assert.deepStrictEqual(Object.keys(g.c._nestedProxyRetries), []);
    assert.ok(!g.timer.running);
    assert.strictEqual(g.decisions.filter(d => d.decision === 0).length, 0,
        'no allow may be sent on a dead connection');
}

// ensures: forgetting a handle on toplevel removal clears its retry even
// when the map entry is the only state (no windows row needed), and a
// later tick issues no broker call for it.
{
    let calls = 0;
    const g = gate({checkPermission: [FAIL, FAIL]});
    const orig = g.binding.checkPermission;
    g.binding.checkPermission = (...a) => { calls++; return FAIL; };
    g.c._decideNestedProxy(10, 'app', 1000);
    g.c._forgetNestedProxyRetry(10);
    g.c._retryNestedProxyDecisions();
    assert.strictEqual(calls, 1, 'no broker call for a removed handle');
    assert.ok(!g.timer.running);
}

// ensures: one eligible handle per tick — a second pending proxy waits for
// the next tick so the synchronous call can't multiply the shell stall.
{
    let calls = 0;
    const g = gate({checkPermission: [FAIL]});
    g.binding.checkPermission = (...a) => { calls++; return FAIL; };
    g.c._decideNestedProxy(11, 'a', 1000);
    g.c._decideNestedProxy(12, 'b', 1000);
    assert.strictEqual(calls, 2);
    g.c._nestedProxyRetries[11].due = 0;
    g.c._nestedProxyRetries[12].due = 0;
    g.c._retryNestedProxyDecisions();
    assert.strictEqual(calls, 3, 'only one handle serviced per tick');
    assert.ok(g.c._nestedProxyRetries[11] && g.c._nestedProxyRetries[12]);
}

// --- Source-level wiring the vm extraction cannot cover ----------------

// ensures: the retry Timer exists, repeats, is driven by the retry tick,
// and uses the configured interval (the vm test calls the tick directly).
{
    const timer = qml.match(
        /Timer \{[^}]*nestedProxyRetryTimer[^}]*\}/);
    assert.ok(timer, 'nestedProxyRetryTimer item must exist');
    assert.ok(timer[0].includes('repeat: true'));
    assert.ok(timer[0].includes('root._retryNestedProxyDecisions()'));
    assert.ok(timer[0].includes('root._nestedProxyRetryIntervalMs'));
}

// ensures: unbind drops all pending retries (connection-scoped state).
{
    const unbound = qml.match(
        /onBoundChanged:[\s\S]*?else \{[^}]*_clearNestedProxyRetries/);
    assert.ok(unbound,
        'onBoundChanged unbound branch must clear nested-proxy retries');
}

// ensures: toplevel removal drops the retry entry unconditionally.
{
    const removed = qml.match(
        /onToplevelRemoved:[\s\S]*?_forgetNestedProxyRetry\(handle\)/);
    assert.ok(removed,
        'onToplevelRemoved must drop pending retries for the handle');
}

console.log('nested-proxy-retry: defer/retry/cleanup invariants passed');
