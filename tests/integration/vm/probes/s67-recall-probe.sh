#!/bin/bash
# v1 Recall cut probe.
#
# Asserts:
#   - the release bootstrap profile did not install/enable the Recall
#     service or timer;
#   - browser-bridge does not register recall.push;
#   - if the dormant SDK is present in a dev image, push_text_snapshot fails
#     closed instead of creating a DB.
set -euo pipefail

PASS() { echo "PASS: $*"; }
FAIL() { echo "FAIL: $*" >&2; exit 1; }

SVC=/etc/systemd/system/qdistro-recall@.service
TMR=/etc/systemd/system/qdistro-recall@.timer

[ ! -e "$SVC" ] || FAIL "$SVC exists in v1 profile"
[ ! -e "$TMR" ] || FAIL "$TMR exists in v1 profile"
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-enabled 'qdistro-recall@admin.timer' >/dev/null 2>&1; then
        FAIL "qdistro-recall@admin.timer is enabled"
    fi
fi
PASS "Recall timer/service not installed in v1 profile"

/usr/bin/python3 - <<'PY'
import importlib.util, sys
# Load the bridge the way it is actually run in production: as a script whose
# own directory is on sys.path[0]. importlib.exec_module does NOT add the file's
# directory automatically, so its sibling import (qdistro_browser_allowlist,
# split out in the P0-4 allowlist follow-up) would otherwise ModuleNotFoundError.
sys.path.insert(0, "/usr/libexec/qdistro")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge",
    "/usr/libexec/qdistro/qdistro_browser_bridge.py")
bb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bb)
identity = {"ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True}
resp = bb.dispatch({"op": "recall.push", "text": "x"}, identity)
assert resp.get("ok") is False, resp
assert resp.get("error") == "unknown_op", resp
assert "recall.push" not in bb.DEFAULT_HANDLERS
PY
PASS "bridge recall.push is not registered"

PY_SITE=$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
SDK_RECALL="$PY_SITE/qdistro_app/recall.py"
if [ -f "$SDK_RECALL" ]; then
    TMP=$(mktemp -d /tmp/s67-recall-cut.XXXXXX)
    trap 'rm -rf "$TMP"' EXIT
    QDISTRO_RECALL_ROOT="$TMP" /usr/bin/python3 - <<PY
import os, sys
sys.path.insert(0, "$PY_SITE")
from qdistro_app import recall
try:
    recall.push_text_snapshot("must not persist", user="admin")
except recall.RecallDisabled:
    pass
else:
    raise AssertionError("expected RecallDisabled")
assert not any(name.endswith(".db") for _root, _dirs, files in os.walk("$TMP") for name in files)
PY
fi
PASS "SDK push_text_snapshot fails closed when present"

PASS "v1 Recall cut probe"
