// Argv builder for qdshell presentation publication.
// Imported by AppPresentationService.qml. Node tests require this file.

"use strict";

function parseOwnerUid(text) {
  var s = String(text || "").trim();
  if (!/^[0-9]+$/.test(s))
    return -1;
  var n = parseInt(s, 10);
  if (!isFinite(n) || n < 0)
    return -1;
  return n;
}

function publishArgv(dir, managedDir, ownerUid) {
  if (!dir)
    return null;
  var cmd = ["qdistro-presentation-publish", "--dir", dir];
  if (dir === managedDir) {
    if (typeof ownerUid !== "number" || !isFinite(ownerUid) || ownerUid < 0)
      return null;
    cmd.push("--owner-uid", String(ownerUid));
  }
  return cmd;
}

if (typeof module !== "undefined") {
  module.exports = {
    parseOwnerUid: parseOwnerUid,
    publishArgv: publishArgv
  };
}
