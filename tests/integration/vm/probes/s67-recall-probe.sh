#!/bin/bash
# spec/17 §step 0 — recall ingest + search end-to-end probe (in-VM).
#
# Asserts:
#   - install-recall-for-vm.sh staged engine + reaper + CLI + SDK
#     + systemd unit/timer.
#   - SDK push_text_snapshot writes to a tmp root.
#   - host CLI search round-trips the SDK-pushed text via FTS5.
#   - browser-bridge recall.push hooked into the dispatch table
#     refuses pwd-domain pushes (advisory layer per spec/17).
#   - reaper drops empty per-day DBs after a forced TTL purge.
#
# Bats wraps this; SKIPs cleanly on bakes that pre-date the install.
set -euo pipefail

PASS() { echo "PASS: $*"; }
SKIP() { echo "SKIP: $*"; exit 0; }

ENG=/usr/libexec/qdistro/qdistro_recall_ingest.py
DAEMON=/usr/libexec/qdistro/qdistro_recall_daemon.py
CLI=/usr/local/bin/qdistro-recall
SVC=/etc/systemd/system/qdistro-recall@.service
TMR=/etc/systemd/system/qdistro-recall@.timer

[ -f "$ENG"    ] || SKIP "$ENG missing"
[ -f "$DAEMON" ] || SKIP "$DAEMON missing"
[ -x "$CLI"    ] || SKIP "$CLI missing"
[ -f "$SVC"    ] || SKIP "$SVC missing"
[ -f "$TMR"    ] || SKIP "$TMR missing"

# SDK presence — locate via the system python's purelib.
PY_SITE=$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
SDK_RECALL="$PY_SITE/qdistro_app/recall.py"
[ -f "$SDK_RECALL" ] || SKIP "$SDK_RECALL missing"

PASS "recall surfaces installed"

TMP=$(mktemp -d /tmp/s67-recall.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
export QDISTRO_RECALL_ROOT="$TMP"

# --- SDK push --------------------------------------------------------
/usr/bin/python3 - <<PY
import os, sys
sys.path.insert(0, "$PY_SITE")
os.environ["QDISTRO_RECALL_ROOT"] = "$TMP"
from qdistro_app import recall
rid = recall.push_text_snapshot(
    "the quick brown fox jumps over the lazy dog",
    user="admin", url="https://qdistro.example/")
print(rid)
PY
PASS "SDK push_text_snapshot inserts a row"

# --- host CLI search round-trip --------------------------------------
OUT=$("$CLI" --root "$TMP" search "quick" --user admin 2>&1)
echo "$OUT" | grep -q "the quick brown fox" \
    || { echo "DEBUG: $OUT"; exit 1; }
PASS "host CLI search returns SDK-pushed text"

# --- pwd-domain refusal at the bridge dispatch (advisory layer) -----
# P0-3 (qdistro@0c3a7a8): the bridge no longer accepts a `user` field
# in the payload — destination user is derived from getpass.getuser()
# of the bridge process. The pwd-domain refusal is orthogonal to that
# and still fires before any user-dir lookup.
/usr/bin/python3 - <<PY
import importlib.util, json, os, sys
sys.path.insert(0, "/usr/libexec/qdistro")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge",
    "/usr/libexec/qdistro/qdistro_browser_bridge.py")
bb = importlib.util.module_from_spec(spec); spec.loader.exec_module(bb)
os.environ["QDISTRO_RECALL_ROOT"] = "$TMP"
identity = {"ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True}
resp = bb.dispatch(
    {"op": "recall.push", "text": "secret pwd UI text",
     "secctx": "qdistro:pwd:fill"},
    identity)
assert resp.get("ok") is False, resp
assert resp.get("error") == "pwd_domain_refused", resp
print("OK")
PY
PASS "bridge recall.push refuses pwd-domain entries"

# --- bridge recall.push happy path inserts a row --------------------
# Destination user comes from the bridge process's UID (here: root,
# since the probe runs under root). Confirm the row is searchable
# under the same user.
BRIDGE_USER=$(/usr/bin/python3 -c 'import getpass; print(getpass.getuser())')
/usr/bin/python3 - <<PY
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge",
    "/usr/libexec/qdistro/qdistro_browser_bridge.py")
bb = importlib.util.module_from_spec(spec); spec.loader.exec_module(bb)
os.environ["QDISTRO_RECALL_ROOT"] = "$TMP"
identity = {"ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True}
resp = bb.dispatch(
    {"op": "recall.push", "text": "browser tab snapshot of a page",
     "url": "https://example.com/"},
    identity)
assert resp.get("ok") is True, resp
assert resp.get("row_id") >= 1, resp
# Regression for P0-3: row MUST land under the bridge's own UID, not
# a stdio-supplied value.
import getpass
assert resp.get("user") == getpass.getuser(), resp
PY
OUT=$("$CLI" --root "$TMP" search "tab snapshot" --user "$BRIDGE_USER" 2>&1)
echo "$OUT" | grep -q "browser tab snapshot" \
    || { echo "DEBUG: $OUT"; exit 1; }
PASS "bridge recall.push inserts a row visible to host CLI search"

# --- reaper drops empty DBs --------------------------------------
/usr/bin/python3 - <<PY
import importlib.util, sqlite3, sys, time, os
spec = importlib.util.spec_from_file_location(
    "qdistro_recall_ingest",
    "/usr/libexec/qdistro/qdistro_recall_ingest.py")
eng = importlib.util.module_from_spec(spec); spec.loader.exec_module(eng)
# Push an old row, then run reaper with ttl_days=1.
path = eng.db_path_for("$TMP", "admin-old")
os.makedirs(os.path.dirname(path), exist_ok=True)
conn = eng.open_db(path)
eng.push_text(conn, user="admin-old", text="old", source="sdk", ts=0.0)
conn.close()
spec2 = importlib.util.spec_from_file_location(
    "qdistro_recall_daemon",
    "/usr/libexec/qdistro/qdistro_recall_daemon.py")
reaper = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(reaper)
stats = reaper.reap("$TMP", "admin-old", 1)
assert stats["purged"] == 1, stats
assert stats["dbs_deleted"] == 1, stats
PY
PASS "reaper purges old + drops empty DBs"

PASS "§spec/17 §step 0 MVP recall ingest probe"
