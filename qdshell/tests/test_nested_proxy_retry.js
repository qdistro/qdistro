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

const entry = (g, handle) =>
    g.c._nestedProxyRetries.find(e => e.handle === handle);

function gate({checkPermission, bound = true} = {}) {
    const decisions = [], log = [], timerCalls = [];
    // Deliberately NO restart(): the product must only start() the shared
    // timer — a restart on every defer would let new arrivals postpone all
    // existing retries forever. If the code calls restart(), this stub
    // throws and the test goes red.
    const timer = {
        running: false,
        start() { this.running = true; timerCalls.push('start'); },
        stop() { this.running = false; timerCalls.push('stop'); },
    };
    const binding = { bound };
    if (checkPermission !== null) {
        const script = checkPermission.slice();
        binding.checkPermission = () =>
            script.length > 1 ? script.shift() : script[0];
    }
    binding.nestedProxyDecision = (handle, decision, reason) =>
        decisions.push({handle, decision, reason});
    const line = (...parts) => log.push(parts.join(' '));
    const ctx = vm.createContext({
        BrokerGate, Date,
        Logger: {i: line, d: line, w: line, e: line},
        qdwinBinding: binding,
        nestedProxyRetryTimer: timer,
        _nestedProxyRetries: [],
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
    assert.strictEqual(entry(g, 7).attempt, 1);
    assert.ok(g.timer.running, 'retry timer must be armed after a defer');
    assert.match(g.gateLines()[0],
        /verdict=defer reason=broker-unavailable attempt=1\/4/);
}

// ensures: a broker that answers on retry releases the proxy with allow and
// the retry entry + timer are cleaned up.
{
    const g = gate({checkPermission: [FAIL, ALLOW]});
    g.c._decideNestedProxy(7, 'org.qd.app', 1000);
    entry(g, 7).due = 0;  // backdate: entry is eligible on the tick
    g.c._retryNestedProxyDecisions();
    assert.deepStrictEqual(g.decisions,
        [{handle: 7, decision: 2, reason: 'broker-unavailable'},
         {handle: 7, decision: 0, reason: 'broker:allow'}]);
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
    assert.ok(!g.timer.running, 'retry timer must stop once empty');
    assert.match(g.gateLines().pop(), /verdict=allow reason=broker:allow/);
}

// ensures: the retry budget is bounded — after _nestedProxyMaxAttempts
// unsuccessful calls the proxy is denied, not deferred forever. Exactly
// maxAttempts broker calls happen.
{
    const calls = [];
    const g = gate({checkPermission: [FAIL]});
    g.binding.checkPermission = (...a) => { calls.push(a); return FAIL; };
    g.c._decideNestedProxy(9, 'app', 1000);           // attempt 1
    for (let i = 0; i < 3; i++) {                     // attempts 2-4
        if (entry(g, 9))
            entry(g, 9).due = 0;
        g.c._retryNestedProxyDecisions();
    }
    assert.strictEqual(calls.length, 4, 'exactly maxAttempts broker calls');
    assert.strictEqual(g.decisions.filter(d => d.decision === 2).length, 3);
    assert.deepStrictEqual(g.decisions[g.decisions.length - 1],
        {handle: 9, decision: 1, reason: 'broker-unavailable'});
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
    assert.match(g.gateLines().pop(),
        /verdict=deny reason=broker-unavailable attempt=4\/4/);
}

// ensures: an explicit broker deny stays immediate — no defer, no retry.
{
    const g = gate({checkPermission: [DENY]});
    g.c._decideNestedProxy(3, 'app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 3, decision: 1, reason: 'broker:deny'}]);
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
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
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
}

// ensures: an unknown verdict word from the broker is terminal deny.
{
    const g = gate({checkPermission: [{exitCode: 0, stdout: 's "pending"\n'}]});
    g.c._decideNestedProxy(5, 'app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 5, decision: 1, reason: 'broker:pending'}]);
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
}

// ensures: a missing checkPermission method (permanent condition) is a
// terminal deny, never deferred.
{
    const g = gate({checkPermission: null});
    g.c._decideNestedProxy(6, 'app', 1000);
    assert.deepStrictEqual(g.decisions,
        [{handle: 6, decision: 1, reason: 'broker-unavailable'}]);
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
    assert.ok(!g.timer.running);
}

// ensures: retries are scoped to the compositor connection — an unbound
// binding drops the whole list and stops the timer (handles may be reused
// by a restarted compositor for different proxies).
{
    const g = gate({checkPermission: [FAIL, ALLOW]});
    g.c._decideNestedProxy(8, 'app', 1000);
    assert.ok(entry(g, 8), 'entry pending');
    g.binding.bound = false;
    g.c._retryNestedProxyDecisions();
    assert.strictEqual(g.c._nestedProxyRetries.length, 0);
    assert.ok(!g.timer.running);
    assert.strictEqual(g.decisions.filter(d => d.decision === 0).length, 0,
        'no allow may be sent on a dead connection');
}

// ensures: forgetting a handle on toplevel removal clears its retry even
// when the list entry is the only state (no windows row needed), and a
// later tick issues no broker call for it.
{
    let calls = 0;
    const g = gate({checkPermission: [FAIL]});
    g.binding.checkPermission = (...a) => { calls++; return FAIL; };
    g.c._decideNestedProxy(10, 'app', 1000);
    g.c._forgetNestedProxyRetry(10);
    g.c._retryNestedProxyDecisions();
    assert.strictEqual(calls, 1, 'no broker call for a removed handle');
    assert.ok(!g.timer.running);
}

// ensures: one eligible handle per tick — a second pending proxy waits for
// the next tick so the synchronous call can't multiply the shell stall;
// and the tick round-robins: after a re-defer the entry moves to the back,
// so the NEXT tick serves the other handle (numeric-key ordering in a map
// would serve the lower handle every tick instead).
{
    const served = [];
    const g = gate({checkPermission: [FAIL]});
    g.binding.checkPermission = () => FAIL;
    g.binding.nestedProxyDecision = (handle, decision, reason) =>
        g.decisions.push({handle, decision, reason});
    g.c._decideNestedProxy(11, 'a', 1000);
    g.c._decideNestedProxy(12, 'b', 1000);
    for (const e of g.c._nestedProxyRetries) e.due = 0;
    // Wrap _decideNestedProxy to record which handle each retry serves.
    const orig = g.c._decideNestedProxy;
    g.c._decideNestedProxy = (h, a, u) => { served.push(h); return orig(h, a, u); };
    for (let i = 0; i < 4; i++) {
        for (const e of g.c._nestedProxyRetries) e.due = 0;
        g.c._retryNestedProxyDecisions();
    }
    assert.deepStrictEqual(served, [11, 12, 11, 12],
        'retries must round-robin, not starve the later handle');
    assert.strictEqual(g.c._nestedProxyRetries.length, 2);
}

// ensures: deferring a NEW proxy does not restart the shared timer and
// postpone already-queued retries — start() is only taken while stopped.
{
    const g = gate({checkPermission: [FAIL]});
    g.c._decideNestedProxy(20, 'a', 1000);   // arms timer
    g.c._decideNestedProxy(21, 'b', 1000);   // second defer while running
    g.c._decideNestedProxy(22, 'c', 1000);
    assert.deepStrictEqual(g.timerCalls, ['start'],
        'a new defer must not restart/reset the shared retry timer');
    // ...and the first-queued entry is still served first on the tick.
    const served = [];
    const orig = g.c._decideNestedProxy;
    g.c._decideNestedProxy = (h, a, u) => { served.push(h); return orig(h, a, u); };
    for (const e of g.c._nestedProxyRetries) e.due = 0;
    g.c._retryNestedProxyDecisions();
    assert.deepStrictEqual(served, [20]);
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
