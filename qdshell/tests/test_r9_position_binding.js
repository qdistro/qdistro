// Source guard for the R9 production cross-output move path.
// Ensures: qdshell can move a floating window across the output seam through
// its exclusive qdwin_shell_v1 binding, without the compositor test hook.
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const header = fs.readFileSync(
  path.join(root, "qml-plugin/qdwin-binding.h"), "utf8");
const source = fs.readFileSync(
  path.join(root, "qml-plugin/qdwin-binding.cpp"), "utf8");
const qml = fs.readFileSync(
  path.join(root, "Services/Qdwin/Qdwin.qml"), "utf8");

assert.ok(header.includes(
  "Q_INVOKABLE void requestSetPosition(quint32 handle, qint32 x, qint32 y);"),
  "native binding must expose the v30 shell-owned position request");
assert.ok(source.includes("if (!shell_ || shellVersion_ < 30)"),
  "position requests must be version-gated at qdwin_shell_v1 v30");
assert.ok(source.includes(
  "qdwin_shell_v1_request_set_position(shell_, handle, x, y);"),
  "position requests must reach qdwin rather than a foreign WM helper");
assert.ok(source.includes("flushAfterRequest(__func__);"),
  "position requests must flush through the binding failure path");
assert.ok(qml.includes("function requestSetPositionHandle(handle, x, y)"),
  "Qdwin singleton must expose the native position request to trusted peers");
assert.ok(qml.includes("root.requestSetPositionHandle(handle, x, y);"),
  "the qdwin IPC operation must route through the bound shell singleton");
assert.ok(!qml.includes("swaymsg") && !qml.includes("hyprctl"),
  "cross-output movement must remain qdwin-only");

console.log("r9-position-binding: qdshell-owned cross-output move path passed");
