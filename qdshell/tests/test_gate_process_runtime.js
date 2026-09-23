// Real Quickshell Process scheduling, using actual gate components and a fake
// busctl executable. No compositor, GUI input, or privileged services involved.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {spawnSync} = require('child_process');
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'qdshell-gate-runtime-'));
try {
    const product = path.join(__dirname, '../Services/Qdshell');
    for (const file of ['ClipboardGate.qml', 'HooksGate.qml', 'ClipboardBroker.js',
        'ClipboardSilo.js', 'ClipboardFocusClear.js', 'ClipboardDenyCoalesce.js', 'BrokerGate.js']) {
        fs.copyFileSync(path.join(product, file), path.join(dir, file));
    }
    fs.writeFileSync(path.join(dir, 'qmldir'), 'singleton ClipboardGate 1.0 ClipboardGate.qml\nsingleton HooksGate 1.0 HooksGate.qml\n');
    fs.mkdirSync(path.join(dir, 'Commons'));
    fs.writeFileSync(path.join(dir, 'Commons/qmldir'), 'singleton Logger 1.0 Logger.qml\n');
    fs.writeFileSync(path.join(dir, 'Commons/Logger.qml'), 'pragma Singleton\nimport QtQuick\nQtObject { function w() {} function e() {} }\n');
    fs.mkdirSync(path.join(dir, 'bin'));
    fs.writeFileSync(path.join(dir, 'bin/busctl'), `#!/usr/bin/python3
import sys, time
args=sys.argv
# Delay to force overlapping requests while each Process is running.
time.sleep(0.05)
if 'VerifyClientIdentity' in args:
    pid=args[args.index('utusssss')+1]
    print('b true' if pid == '101' else 'b false')
elif 'RequestPermission' in args:
    print('s "allow"')
else:
    print('s "allow"' if args[-1] == 'first' else 's "unknown"')
`, {mode: 0o755});
    for (const order of [[11, 22], [22, 11]]) {
    fs.writeFileSync(path.join(dir, 'shell.qml'), `import QtQuick
import Quickshell
import "."
Scope {
    id: test
    property int allowedA: 0
    property int allowedB: 0
    property int ticks: 0
    function identity(pid) {
        return {pid: pid, starttime: 123, uid: 1000, exe: "/app", label: "",
            sandboxEngine: "qdistro", appId: "work", instanceId: "launch"};
    }
    Component.onCompleted: {
        ClipboardGate._handleToIdentity = {11: identity(101), 22: identity(202)};
        ClipboardGate._ensureVerified(${order[0]});
        ClipboardGate._ensureVerified(${order[1]});
        HooksGate.gate("startup", "first", () => test.allowedA++);
        HooksGate.gate("startup", "second", () => test.allowedB++);
    }
    Timer {
        interval: 20; running: true; repeat: true
        onTriggered: {
            test.ticks++;
            const a = ClipboardGate._verifyCache[ClipboardGate._verifyKey(test.identity(101))];
            const b = ClipboardGate._verifyCache[ClipboardGate._verifyKey(test.identity(202))];
            if (a && b && !HooksGate._active && HooksGate._queue.length === 0) {
                if (a.verified === true && b.verified === false && test.allowedA === 1 && test.allowedB === 0
                    && Object.keys(ClipboardGate._verifyInFlight).length === 0)
                    console.log("REAL_GATE_PROCESS_PASS");
                else console.error("REAL_GATE_PROCESS_FAIL", JSON.stringify(a), JSON.stringify(b), test.allowedA, test.allowedB);
                Qt.quit();
            } else if (test.ticks > 200) {
                console.error("REAL_GATE_PROCESS_TIMEOUT");
                Qt.quit();
            }
        }
    }
}
`);
    const run = spawnSync('quickshell', ['--no-color', '--path', path.join(dir, 'shell.qml')], {
        env: {...process.env, QT_QPA_PLATFORM: 'offscreen', PATH: path.join(dir, 'bin') + ':' + process.env.PATH},
        encoding: 'utf8', timeout: 10000
    });
    const output = (run.stdout || '') + (run.stderr || '');
    // ensures: real child exits preserve identity/callback ownership, and unknown never executes.
    assert.strictEqual(run.error, undefined, String(run.error) + '\n' + output);
    assert.strictEqual(run.status, 0, output);
    assert.ok(output.includes('REAL_GATE_PROCESS_PASS'), output);
    }
    console.log('real Quickshell Process gates PASS (both verification orders)');
} finally {
    fs.rmSync(dir, {recursive: true, force: true});
}
