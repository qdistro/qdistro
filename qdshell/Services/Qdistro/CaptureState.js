// Live-capture state derivation for the lock-screen indicators (J28).
//
// WHY THIS EXISTS: `sessions.md` mandates non-suppressible lock-surface
// indicators for live microphone, camera, screencast, system-audio capture and
// virtual input. There is no single authoritative "who is capturing" feed in
// qdistro today: qdwin knows its weston_capture_v1 / view-stream clients but
// emits no event for them, and the session manager has no capture surface. The
// one graph that DOES see cross-silo capture is PipeWire — silos are given a
// bind-mounted view of admin's `pipewire-0` socket (doc/games.md,
// doc/isolation-tiers.md), and tier-5 VMs route audio through the host daemon
// with `-audiodev pipewire` — so every capture stream that goes through
// PipeWire lands in the one graph qdshell can already read with `pw-dump`.
//
// FAIL VISIBLE, NOT FAIL SILENT: what PipeWire gives us is a *positive*
// signal — selected running nodes are evidence of capture activity. Where the
// node is a client stream the client is named; where it is only a running
// source *device* node the activity is real but the client is NOT
// established. (qdlocker's copy labels that difference in the UI; this one
// does not — see the experimental note below.) The *negative* is not
// trustworthy for ANY kind today:
//
//   - camera: a policy-approved fullscreen session can hold a direct device
//     grant (doc/devices.md) and open `/dev/videoN` without PipeWire.
//   - microphone / system audio: same — direct ALSA (`audio` group +
//     `/dev/snd/*` ACL) is a documented opt-in for fullscreen game sessions
//     (doc/games.md), and per-session PipeWire daemons link *upward* into
//     admin's graph (doc/devices.md), so a capture can be represented by a
//     link on a virtual source rather than by a `Stream/Input/Audio` node we
//     recognise.
//   - screencast: qdwin's view-stream path publishes a `weston.pipewire-N`
//     node we can see, but a direct `weston_capture_v1` grab does not.
//   - virtual input / accessibility control: qdwin filters the
//     `zwp_virtual_keyboard` / `zwp_input_method` globals but reports nothing,
//     so there is no observer at all.
//
// So NEGATIVE_AUTHORITATIVE is false for every kind: this code can say "X is
// capturing", never "nothing is capturing". Absence of evidence renders as a
// visible "unverified" (a "?" on the lock surface), never as a quiet
// all-clear, and a dead or stale observer drives every kind there too. Making
// a kind report "clear" requires a real authoritative feed first (a qdwin
// capture/virtual-input event, or a device-grant registry) — flipping the flag
// without one is how this becomes a lie. tests/test_capture_state.js pins it.
//
// STATUS: EXPERIMENTAL, NOT A LOCK GUARANTEE. The real lock surface is
// qdlocker (qdlocker/qdlocker/indicators.py); qdshell's own lock screen is the
// deprecated WlSessionLock path qdwin does not implement, so nothing
// instantiates this today. It is NOT equivalent to qdlocker's copy: that one
// streams stdout under a hard byte cap, binds every scan callback to the exact
// process object, labels device-only evidence as unattributed, and counts
// `Stopping` silos as live egress. Reconcile the two before any consumer
// (e.g. an unlocked-session bar indicator) instantiates this service.
//
// See doc/sessions.md in the qdistro repo for the contract.
"use strict";

// Ordered for display; also the iteration order of every returned map.
// `unattributed` catches capture-shaped nodes whose medium we cannot pin down
// (a bare `Stream/Input`, a `media.category=Capture` node with no class): the
// node is still evidence that SOMETHING is capturing, and dropping it would be
// exactly the silent failure this file exists to prevent.
var KINDS = ["microphone", "camera", "screencast", "systemAudio",
             "virtualInput", "unattributed"];

var KIND_LABELS = {
  microphone: "mic",
  camera: "camera",
  screencast: "screen",
  systemAudio: "system audio",
  virtualInput: "virtual input",
  unattributed: "capture",
};

var KIND_ICONS = {
  microphone: "microphone",
  camera: "camera",
  screencast: "screen-share",
  systemAudio: "volume",
  virtualInput: "keyboard",
  unattributed: "alert-triangle",
};

// Can "we saw no evidence" be reported as "clear"? Every kind has a blind spot
// (see the header), so today the answer is no for all of them: absence of
// evidence renders as "unverified". The map is kept per-kind so that a kind
// whose authoritative feed lands later can be flipped one at a time.
var NEGATIVE_AUTHORITATIVE = {
  microphone: false,
  camera: false,
  screencast: false,
  systemAudio: false,
  virtualInput: false,
  unattributed: false,
};

function truthy(value) {
  if (value === true)
    return true;
  if (value === false || value === null || value === undefined)
    return false;
  var s = String(value).toLowerCase();
  return s !== "" && s !== "0" && s !== "false" && s !== "no";
}

// Parse `pw-dump` output. Returns {ok, nodes}: ok=false means "the observer did
// not produce a usable graph" (missing binary, error, truncated JSON) and MUST
// be rendered as unverified, not as an empty/quiet graph.
function parsePwDump(raw) {
  if (raw === null || raw === undefined)
    return { ok: false, nodes: [] };
  var text = String(raw).trim();
  if (text === "")
    return { ok: false, nodes: [] };
  var parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    return { ok: false, nodes: [] };
  }
  if (!Array.isArray(parsed))
    return { ok: false, nodes: [] };
  var nodes = [];
  var pipewireObjects = 0;
  for (var i = 0; i < parsed.length; i++) {
    var obj = parsed[i] || {};
    var type = String(obj.type || "");
    if (type.indexOf("PipeWire:Interface:") !== 0)
      continue;
    pipewireObjects++;
    if (type.indexOf("Interface:Node") === -1)
      continue;
    var info = obj.info || {};
    nodes.push({
      id: Number(obj.id || 0),
      state: String(info.state || ""),
      props: info.props || {},
    });
  }
  // A real dump always contains PipeWire objects (Core, Client, Node…). An
  // empty array, `[{}]`, or an array of unrelated JSON is NOT "a quiet graph",
  // it is a failed observation, and must not be allowed to stand in for one.
  if (pipewireObjects === 0)
    return { ok: false, nodes: [] };
  return { ok: true, nodes: nodes };
}

function nodeApp(props) {
  var name = String(props["application.name"] || props["node.description"] ||
                    props["node.name"] || "").trim();
  return name;
}

// Classify one PipeWire node into a capture kind, or null when it is not
// capture-relevant. Only `running` nodes are evidence: an idle or suspended
// stream is connected but not moving samples/frames. Any other state string
// (including one we do not recognise) is treated as no evidence — which is
// safe here ONLY because no kind can report "clear" on the strength of absent
// evidence; see NEGATIVE_AUTHORITATIVE.
function classifyNode(node) {
  node = node || {};
  var props = node.props || {};
  if (String(node.state || "") !== "running")
    return null;

  var mediaClass = String(props["media.class"] || "");
  var mediaType = String(props["media.type"] || "").toLowerCase();
  var category = String(props["media.category"] || "").toLowerCase();
  var name = String(props["node.name"] || "");
  var role = String(props["media.role"] || "").toLowerCase();
  var api = String(props["device.api"] || "").toLowerCase();
  var app = nodeApp(props);
  var haystack = (name + " " + app).toLowerCase();
  var cameraish = role === "camera" || api === "v4l2" || api === "libcamera" ||
      haystack.indexOf("camera") !== -1 || haystack.indexOf("webcam") !== -1 ||
      haystack.indexOf("v4l2") !== -1;

  // qdwin pins a forwarded toplevel onto a weston backend-pipewire output and
  // names the node `weston.pipewire-N` (qdwin.c, qdwin_view_stream_v1). A live
  // one means a screencast/remote-display stream is running right now.
  if (name.indexOf("weston.pipewire") === 0)
    return { kind: "screencast", app: app };

  var isCaptureStream = mediaClass.indexOf("Stream/Input") === 0 ||
      category === "capture";
  if (isCaptureStream) {
    var video = mediaClass.indexOf("Video") !== -1 || mediaType === "video";
    var audio = mediaClass.indexOf("Audio") !== -1 || mediaType === "audio";
    if (video)
      return { kind: cameraish ? "camera" : "screencast", app: app };
    if (audio) {
      return {
        kind: truthy(props["stream.capture.sink"]) ? "systemAudio" : "microphone",
        app: app,
      };
    }
    // Capture-shaped but we cannot tell what it captures. Still evidence.
    return { kind: "unattributed", app: app };
  }

  // A producing video stream that is not a camera is a screen source (this is
  // what a compositor-side screencast producer looks like).
  if (mediaClass === "Stream/Output/Video")
    return { kind: cameraish ? "camera" : "screencast", app: app };

  // Device-side nodes only run while something is pulling from them, so a
  // running source device is itself evidence of capture — including capture
  // that reaches admin's graph through a per-session PipeWire linking upward,
  // where the client-side stream node is not visible here.
  if (mediaClass === "Audio/Source")
    return { kind: "microphone", app: app };
  // A video *device* source defaults the other way from a video *stream*: a
  // device node is a camera unless it names itself as a screen/compositor
  // source, whereas a client's video capture stream is usually a screencast.
  if (mediaClass === "Video/Source") {
    var screenish = haystack.indexOf("screen") !== -1 ||
        haystack.indexOf("desktop") !== -1 || haystack.indexOf("weston") !== -1 ||
        haystack.indexOf("monitor") !== -1;
    return { kind: screenish && !cameraish ? "screencast" : "camera", app: app };
  }

  return null;
}

// Deduped, sorted capture entries. Producer and consumer nodes of the same
// screencast share an app name, so dedupe on kind+app keeps one row per
// observable capture rather than double-counting each end of the link.
function captureEntries(nodes) {
  var out = [];
  var seen = {};
  if (!Array.isArray(nodes))
    return out;
  for (var i = 0; i < nodes.length; i++) {
    var hit = classifyNode(nodes[i]);
    if (!hit)
      continue;
    var app = hit.app || "";
    var key = hit.kind + "\u0000" + app;
    if (seen[key])
      continue;
    seen[key] = true;
    out.push({ kind: hit.kind, app: app, label: KIND_LABELS[hit.kind] });
  }
  out.sort(function (a, b) {
    if (a.kind !== b.kind)
      return KINDS.indexOf(a.kind) - KINDS.indexOf(b.kind);
    return a.app.localeCompare(b.app);
  });
  return out;
}

function entryText(entry) {
  return entry.app ? (entry.label + ":" + entry.app) : entry.label;
}

// Derive the lock-surface state.
//
// `parsed` is a parsePwDump() result; `opts.fresh` is false when the last
// successful scan is too old to trust (observer wedged, or the lock surface
// has not been re-scanned since it appeared). Either failure drives EVERY kind
// to "unverified" — an unobserved machine must never render as a quiet one.
function summarise(parsed, opts) {
  opts = opts || {};
  var fresh = opts.fresh === undefined ? true : !!opts.fresh;
  var limit = Math.max(1, Number(opts.limit || 2));
  var ok = !!(parsed && parsed.ok) && fresh;
  var entries = ok ? captureEntries(parsed.nodes) : [];

  var kinds = {};
  var active = [];
  var unverified = [];
  for (var i = 0; i < KINDS.length; i++) {
    var kind = KINDS[i];
    var mine = entries.filter(function (e) {
      return e.kind === kind;
    });
    var state;
    if (!ok)
      state = "unverified";
    else if (mine.length > 0)
      state = "active";
    else
      state = NEGATIVE_AUTHORITATIVE[kind] ? "clear" : "unverified";
    kinds[kind] = {
      kind: kind,
      state: state,
      icon: KIND_ICONS[kind],
      label: KIND_LABELS[kind],
      count: mine.length,
      apps: mine.map(function (e) {
        return e.app;
      }),
      detail: mine.map(entryText).join(", "),
    };
    if (state === "active")
      active.push(kind);
    else if (state === "unverified")
      unverified.push(kind);
  }

  var shown = [];
  for (var j = 0; j < Math.min(limit, entries.length); j++)
    shown.push(entryText(entries[j]));
  var extra = entries.length - shown.length;

  return {
    observerOk: ok,
    activeKinds: active,
    activeCount: entries.length,
    activeLabel: extra > 0 ? shown.join(", ") + " +" + extra : shown.join(", "),
    activeDetail: entries.map(entryText).join(", "),
    anyActive: entries.length > 0,
    unverifiedKinds: unverified,
    unverifiedLabel: unverified.map(function (k) {
      return KIND_LABELS[k];
    }).join(", "),
    anyUnverified: unverified.length > 0,
    kinds: kinds,
    // The lock surface shows the cluster whenever there is anything to say —
    // and there always is, because virtualInput has no observer at all. This
    // is deliberate: the indicator is non-suppressible by design.
    visible: entries.length > 0 || unverified.length > 0,
  };
}

var api = {
  KINDS: KINDS,
  KIND_LABELS: KIND_LABELS,
  KIND_ICONS: KIND_ICONS,
  NEGATIVE_AUTHORITATIVE: NEGATIVE_AUTHORITATIVE,
  parsePwDump: parsePwDump,
  classifyNode: classifyNode,
  captureEntries: captureEntries,
  summarise: summarise,
};

if (typeof module !== "undefined")
  module.exports = api;
