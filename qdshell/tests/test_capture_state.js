// Lock-screen live-capture indicator logic (J28).
//
// The security property under test is FAIL VISIBLE: every path that does not
// positively establish "nothing is capturing" must render as "unverified", and
// never as a quiet/clear indicator. That covers a dead observer, a stale scan,
// unparsable output, and the kinds whose negative qdistro cannot observe at all
// (camera via a direct /dev/videoN open, a weston_capture_v1 screen grab,
// virtual input — which has no observer whatsoever).
"use strict";

const assert = require("assert");
const C = require("../Services/Qdistro/CaptureState.js");

function node(state, props) {
  return { type: "PipeWire:Interface:Node", id: 1, info: { state: state, props: props } };
}
function dump(objs) {
  return JSON.stringify(objs);
}

// ─── parsing ────────────────────────────────────────────────────────────────
// A failed observation must be distinguishable from an empty graph.
assert.strictEqual(C.parsePwDump("").ok, false);
assert.strictEqual(C.parsePwDump(null).ok, false);
assert.strictEqual(C.parsePwDump(undefined).ok, false);
assert.strictEqual(C.parsePwDump("not json").ok, false);
assert.strictEqual(C.parsePwDump('{"id":1}').ok, false, "a non-array dump is not a graph");
// A real dump always carries PipeWire objects. An empty array, an array of
// empty objects, or unrelated JSON is a FAILED observation, not a quiet graph —
// otherwise a redacted/truncated/mocked dump could stand in for one.
assert.strictEqual(C.parsePwDump("[]").ok, false, "an empty array is not a graph");
assert.strictEqual(C.parsePwDump("[{}]").ok, false);
assert.strictEqual(C.parsePwDump('[{"type":"something-else"}]').ok, false);
assert.strictEqual(C.parsePwDump('[{"type":"PipeWire:Interface:Core","id":0}]').ok, true,
  "a graph with no nodes but real PipeWire objects IS an observation");

const mixed = C.parsePwDump(dump([
  { type: "PipeWire:Interface:Link", id: 7 },
  node("running", { "media.class": "Audio/Sink", "node.name": "sink" }),
]));
assert.strictEqual(mixed.ok, true);
assert.strictEqual(mixed.nodes.length, 1, "non-node objects are dropped");
assert.strictEqual(mixed.nodes[0].state, "running");

// ─── classification ─────────────────────────────────────────────────────────
// Only running nodes are evidence — idle/suspended streams are connected but
// not moving samples.
assert.strictEqual(
  C.classifyNode({ state: "idle", props: { "media.class": "Stream/Input/Audio" } }), null);
assert.strictEqual(
  C.classifyNode({ state: "suspended", props: { "media.class": "Stream/Input/Video" } }), null);

assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Audio" } }).kind,
  "microphone");
// A monitor/loopback capture of the sink is system-audio capture, not the mic.
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Audio", "stream.capture.sink": "1" } }).kind,
  "systemAudio");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Audio", "stream.capture.sink": true } }).kind,
  "systemAudio");
// "false"/"0"/"" must not be read as truthy by the string-valued prop.
["false", "0", "", "no"].forEach(function (v) {
  assert.strictEqual(
    C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Audio", "stream.capture.sink": v } }).kind,
    "microphone", "stream.capture.sink=" + JSON.stringify(v) + " must not mean system audio");
});

// Video: camera-ish props win, everything else is treated as screencast.
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Video", "media.role": "Camera" } }).kind,
  "camera");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Video", "device.api": "libcamera" } }).kind,
  "camera");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Video/Source", "device.api": "v4l2" } }).kind,
  "camera");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input/Video", "application.name": "obs" } }).kind,
  "screencast");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Output/Video", "node.name": "kwin_wayland" } }).kind,
  "screencast");
// qdwin pins a forwarded toplevel onto a weston backend-pipewire output; a live
// `weston.pipewire-N` node means a remote-display/screencast stream is running.
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "node.name": "weston.pipewire-0" } }).kind,
  "screencast");
// Plain playback is not capture.
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Output/Audio", "application.name": "mpv" } }), null);
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Audio/Sink" } }), null);

// Capture that reaches admin's graph without a recognisable stream node: a
// running source device is itself evidence (per-session PipeWire daemons link
// upward into admin's graph, so the client-side node may not be visible here).
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Audio/Source", "node.description": "Built-in Mic" } }).kind,
  "microphone");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Video/Source", "node.name": "cam0" } }).kind,
  "camera");
// Capture-shaped but unclassifiable nodes are still reported, never dropped.
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.class": "Stream/Input", "application.name": "mystery" } }).kind,
  "unattributed");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.category": "Capture", "application.name": "mystery" } }).kind,
  "unattributed");
// media.type disambiguates a class-less capture node.
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.category": "Capture", "media.type": "Audio" } }).kind,
  "microphone");
assert.strictEqual(
  C.classifyNode({ state: "running", props: { "media.category": "Capture", "media.type": "Video" } }).kind,
  "screencast");

// ─── entries: dedupe + ordering ─────────────────────────────────────────────
const entries = C.captureEntries(C.parsePwDump(dump([
  node("running", { "media.class": "Stream/Input/Video", "application.name": "obs" }),
  node("running", { "media.class": "Stream/Output/Video", "application.name": "obs" }),
  node("running", { "media.class": "Stream/Input/Audio", "application.name": "zoom" }),
  node("running", { "media.class": "Stream/Input/Audio", "application.name": "meet" }),
  node("idle", { "media.class": "Stream/Input/Audio", "application.name": "quiet" }),
])).nodes);
assert.deepStrictEqual(entries.map(e => e.kind + ":" + e.app),
  ["microphone:meet", "microphone:zoom", "screencast:obs"],
  "kind order, then app order, with both ends of one screencast deduped");

// ─── summarise: the fail-visible contract ───────────────────────────────────
// 1. Observer failed → every kind unverified, nothing reads as clear.
const dead = C.summarise(C.parsePwDump(""), { fresh: true });
assert.strictEqual(dead.observerOk, false);
assert.strictEqual(dead.anyActive, false);
assert.strictEqual(dead.activeCount, 0);
assert.deepStrictEqual(dead.unverifiedKinds, C.KINDS, "a dead observer leaves NOTHING clear");
assert.strictEqual(dead.anyUnverified, true);
assert.strictEqual(dead.visible, true, "a dead observer must still show the indicator");
C.KINDS.forEach(function (k) {
  assert.strictEqual(dead.kinds[k].state, "unverified", k + " must not read as clear");
});

// 2. Stale scan → same, even though the parse succeeded.
const stale = C.summarise(C.parsePwDump(dump([
  node("running", { "media.class": "Stream/Input/Audio", "application.name": "zoom" }),
])), { fresh: false });
assert.strictEqual(stale.observerOk, false);
assert.strictEqual(stale.anyActive, false, "stale evidence is not live evidence");
assert.deepStrictEqual(stale.unverifiedKinds, C.KINDS);
assert.strictEqual(stale.visible, true);

// 3. Healthy observer, quiet graph → still no all-clear. No kind has an
//    authoritative negative today (direct /dev/snd and /dev/video grants,
//    weston_capture_v1 grabs, per-session PipeWire daemons linking upward, and
//    virtual input with no observer at all), so a quiet graph reads
//    "unverified" everywhere rather than "nothing is capturing".
const quiet = C.summarise(C.parsePwDump('[{"type":"PipeWire:Interface:Core","id":0}]'), { fresh: true });
assert.strictEqual(quiet.observerOk, true);
assert.strictEqual(quiet.anyActive, false);
assert.deepStrictEqual(quiet.unverifiedKinds, C.KINDS);
assert.strictEqual(quiet.unverifiedLabel,
  "mic, camera, screen, system audio, virtual input, capture");
assert.strictEqual(quiet.visible, true,
  "the cluster is never suppressed while anything is unverified");
C.KINDS.forEach(function (k) {
  assert.notStrictEqual(quiet.kinds[k].state, "clear",
    k + " must not claim an all-clear it cannot observe");
});

// 4. Live capture → active kinds, counts, and a truncated label.
const live = C.summarise(C.parsePwDump(dump([
  node("running", { "media.class": "Stream/Input/Audio", "application.name": "zoom" }),
  node("running", { "media.class": "Stream/Input/Video", "media.role": "Camera", "application.name": "zoom" }),
  node("running", { "node.name": "weston.pipewire-0", "application.name": "weston" }),
])), { fresh: true, limit: 2 });
assert.strictEqual(live.observerOk, true);
assert.strictEqual(live.anyActive, true);
assert.strictEqual(live.activeCount, 3);
assert.deepStrictEqual(live.activeKinds, ["microphone", "camera", "screencast"]);
assert.strictEqual(live.activeLabel, "mic:zoom, camera:zoom +1");
assert.strictEqual(live.activeDetail, "mic:zoom, camera:zoom, screen:weston");
assert.strictEqual(live.kinds.microphone.state, "active");
assert.strictEqual(live.kinds.microphone.count, 1);
assert.strictEqual(live.kinds.camera.detail, "camera:zoom");
// The kinds with no evidence stay unverified alongside the active rows: an
// active mic does not license an implicit "and nothing else is capturing".
assert.strictEqual(live.kinds.systemAudio.state, "unverified");
assert.deepStrictEqual(live.unverifiedKinds, ["systemAudio", "virtualInput", "unattributed"]);
assert.strictEqual(live.anyUnverified, true);

// 5. A kind that IS active is never simultaneously reported unverified.
C.KINDS.forEach(function (k) {
  assert.ok(!(live.activeKinds.indexOf(k) !== -1 && live.unverifiedKinds.indexOf(k) !== -1),
    k + " cannot be both active and unverified");
});

// 6. Every kind has display metadata the lock panel can render.
C.KINDS.forEach(function (k) {
  assert.ok(C.KIND_LABELS[k], "missing label for " + k);
  assert.ok(C.KIND_ICONS[k], "missing icon for " + k);
  assert.ok(quiet.kinds[k].icon === C.KIND_ICONS[k]);
  assert.ok(typeof C.NEGATIVE_AUTHORITATIVE[k] === "boolean");
});
// Pinned: NO kind may claim an authoritative negative. Flipping one turns a
// visible "?" into a silent all-clear, which needs a real authoritative feed
// (a qdwin capture/virtual-input event, or a device-grant registry) first.
assert.deepStrictEqual(C.KINDS.filter(k => C.NEGATIVE_AUTHORITATIVE[k]), []);

// 7. Defaults: summarise() with no opts treats the reading as fresh, and a
//    missing/garbage parse result still fails visible.
assert.strictEqual(C.summarise(null).observerOk, false);
assert.deepStrictEqual(C.summarise(undefined).unverifiedKinds, C.KINDS);
assert.strictEqual(
  C.summarise(C.parsePwDump('[{"type":"PipeWire:Interface:Core","id":0}]')).kinds.microphone.state,
  "unverified");

// ─── lock-panel wiring ──────────────────────────────────────────────────────
// Host qmllint cannot resolve the `qs.*` module imports (that needs a GUI VM),
// so these are source-level invariants on the QML rather than a real QML lint:
// they pin the wiring that makes the indicator non-suppressible and post-lock.
const fs = require("fs");
const path = require("path");
const ROOT = path.resolve(__dirname, "..");
const panel = fs.readFileSync(
  path.join(ROOT, "Modules", "LockScreen", "LockScreenPanel.qml"), "utf8");
const service = fs.readFileSync(
  path.join(ROOT, "Services", "Qdistro", "CaptureStateService.qml"), "utf8");

assert.ok(/import qs\.Services\.Qdistro/.test(panel),
  "lock panel must import the service module");
assert.ok(panel.includes("CaptureStateService.markStale()") &&
          panel.includes("CaptureStateService.refresh()"),
  "lock panel must re-observe after the lock instead of inheriting pre-lock state");
// Compact mode: the container is sized and shown for the capture cluster too,
// so it cannot be squeezed out by the other indicators being absent.
assert.ok((panel.match(/CaptureStateService\.indicatorVisible/g) || []).length >= 3,
  "capture cluster must drive compact width, compact visibility and full-mode visibility");
assert.ok(panel.includes("CaptureStateService.activeKinds") &&
          panel.includes("CaptureStateService.anyUnverified"),
  "full panel must render both the active kinds and the unverified ones");
// Non-suppressible: no Settings flag may gate a capture row's visibility.
const captureVisibility = (panel.match(/visible:[^\n]*CaptureStateService[^\n]*/g) || []);
assert.ok(captureVisibility.length >= 3, "expected the capture visibility bindings");
captureVisibility.forEach(function (line) {
  assert.ok(!/Settings\.data\.general\.(?!compactLockScreen)/.test(line),
    "capture indicators must not be gated by a settings toggle: " + line.trim());
});

// A blanked output (lockScreenMonitors excludes it) must still carry the
// indicators — otherwise a cosmetic setting suppresses a security signal.
const lockScreen = fs.readFileSync(
  path.join(ROOT, "Modules", "LockScreen", "LockScreen.qml"), "utf8");
const blackComponent = lockScreen.slice(lockScreen.indexOf("id: blackScreenComponent"));
assert.ok(blackComponent.includes("LockSecurityIndicators"),
  "the blacked-out lock output must still render the security indicators");
assert.ok(fs.existsSync(path.join(ROOT, "Modules", "LockScreen", "LockSecurityIndicators.qml")));

// Observer invariants that keep the indicator honest across scans:
assert.ok(/readonly property bool fresh: hasReading && ageMs <= staleAfterMs/.test(service),
  "freshness must require a reading that has not aged out");
assert.ok(/root\.ageMs \+= interval;/.test(service) && !/Date\.now\(\)/.test(service),
  "age must be counted in timer ticks, not wall clock (a clock jump must not " +
  "extend the trusted window)");
assert.ok(/if \(gen !== generation \|\| gen !== _launchGen\)\s*\n\s*return;/.test(service),
  "a scan launched before the last markStale()/refresh() must be discarded");
assert.ok(/if \(exitCode !== 0\)/.test(service) &&
          /_pendingText === null \|\| _pendingExit === null/.test(service),
  "a scan is accepted only on a zero exit AND complete stdout");
assert.ok(/raw\.length > maxDumpBytes/.test(service),
  "oversized output must be rejected, not parsed on the UI thread");
assert.ok(/exec pw-dump/.test(service) && !/pw-dump \|\| true/.test(service),
  "the scanner must be the direct child (so a kill reaches it) and must not " +
  "mask its exit status");
assert.ok(/id: _scanTimeout/.test(service) && /_scan\.running = false;/.test(service),
  "a wedged scan must be killed rather than left holding the reading");
assert.ok(/function markStale\(\) \{\s*\n\s*generation\+\+;/.test(service),
  "markStale must invalidate in-flight scans, not just the stored reading");

console.log("capture-state: all assertions passed");
process.exit(0);
