const assert = require("assert");
const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "..");
const qml = fs.readFileSync(
    path.join(repo, "Services/Qdwin/Qdwin.qml"), "utf8");
const handler = qml.match(
    /onNestedProxyPixelSource:[\s\S]*?\n        onToplevelRemoved:/
);

assert.ok(handler, "nested proxy pixel-source handler should exist");

// Ensures: the production tier-2 nested display uses the reliable SHM lane;
// the opt-in dmabuf path must not crash the inner compositor underneath it.
assert.ok(
    handler[0].includes(
        '["/usr/bin/env", "QDWIN_PIXELFEED_NO_DMABUF=1",\n' +
        '                          "/usr/bin/qdistro-nested-pixelfeed",\n' +
        '                          String(handle), pwNode]'
    ),
    "nested pixelfeed launch must pin SHM and use root-installed binaries"
);

// Ensures: argv stays tokenized; a protocol-provided node string is never
// interpolated into a shell command while applying the environment override.
assert.ok(
    handler[0].includes("Quickshell.execDetached(argv)"),
    "nested pixelfeed must use tokenized execDetached argv"
);
assert.ok(
    !handler[0].includes('execDetached(["sh"') &&
    !handler[0].includes('execDetached(["bash"'),
    "nested pixelfeed must not route protocol strings through a shell"
);

// Ensures: an R6 remote proxy selects only the dedicated decoder-owned feeder;
// a protocol string cannot become a path, executable, or shell fragment.
assert.ok(
    handler[0].includes(
        'if (!/^qdistro\\.remote:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(pwNode))'
    ),
    "remote pixel source must have a strict opaque token"
);
assert.ok(
    handler[0].includes(
        'argv = ["/usr/bin/qdistro-mm-remote-pixelfeed",\n' +
        '                          String(handle), pwNode]'
    ),
    "remote pixels must use the root-installed remote SHM feeder"
);
assert.ok(
    !handler[0].includes("qdistro-mm-remote-pixelfeed\", inputSink"),
    "remote pixelfeed must not consume the advertised QDNI Unix path"
);

console.log("nested-pixelfeed-launch: SHM and tokenized-argv invariants passed");
