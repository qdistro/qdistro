#!/bin/bash
# spec/14 Phase-8 MVP — browser-bridge in-VM probe.
#
# Asserts:
#   - install-browser-bridge-for-vm.sh staged the right files at the
#     spec/14-pinned paths.
#   - qdistro-browser-install --print round-trips the right manifest
#     shapes (Firefox=allowed_extensions, Chromium-family=allowed_origins).
#   - The bridge stub at /usr/lib/qdistro/browser-bridge round-trips
#     a qdistro.ping JSON message via stdin/stdout 4-byte length-prefix
#     framing under a stubbed parent-allowlist (Firefox /usr/lib64/firefox/firefox).
#   - Without parent-allowlist match, the bridge replies with a clean
#     `parent_not_allowed` body — no traceback, no crash.
#
# This sits at the lowest layer that retires the architecture risk:
# we don't drive a real browser (deferred to release-time AMO sign
# path), but we do prove the binary works as native-messaging hosts
# are expected to.
set -euo pipefail

PASS() { echo "PASS: $*"; }
SKIP() { echo "SKIP: $*"; exit 0; }

BRIDGE=/usr/lib/qdistro/browser-bridge
INSTALL_CLI=/usr/local/bin/qdistro-browser-install
MOD=/usr/libexec/qdistro/qdistro_browser_bridge.py
INSTALL_MOD=/usr/libexec/qdistro/qdistro_browser_install.py

[ -x "$BRIDGE"      ] || SKIP "$BRIDGE missing"
[ -x "$INSTALL_CLI" ] || SKIP "$INSTALL_CLI missing"
[ -f "$MOD"         ] || SKIP "$MOD missing"
[ -f "$INSTALL_MOD" ] || SKIP "$INSTALL_MOD missing"

PASS "browser-bridge surfaces installed"

# --- manifest shape via --print ----
TMP=$(mktemp -d /tmp/s66-bb.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

OUT=$("$INSTALL_CLI" --home "$TMP" --browsers firefox --print 2>&1)
echo "$OUT" | grep -q '"name": "qdistro"'           || { echo "DEBUG: $OUT"; exit 1; }
echo "$OUT" | grep -q '"allowed_extensions"'        || { echo "DEBUG: $OUT"; exit 1; }
echo "$OUT" | grep -q '"path": "/usr/lib/qdistro/browser-bridge"' || { echo "DEBUG: $OUT"; exit 1; }
PASS "Firefox manifest shape via --print"

OUT=$("$INSTALL_CLI" --home "$TMP" --browsers chromium --print 2>&1)
echo "$OUT" | grep -q '"allowed_origins"'           || { echo "DEBUG: $OUT"; exit 1; }
echo "$OUT" | grep -q 'chrome-extension://'         || { echo "DEBUG: $OUT"; exit 1; }
PASS "Chromium manifest shape via --print"

# --- write all six ----
"$INSTALL_CLI" --home "$TMP" >/dev/null
for sub in \
    .mozilla/native-messaging-hosts/qdistro.json \
    .config/chromium/NativeMessagingHosts/qdistro.json \
    .config/google-chrome/NativeMessagingHosts/qdistro.json \
    .config/BraveSoftware/Brave-Browser/NativeMessagingHosts/qdistro.json \
    .config/vivaldi/NativeMessagingHosts/qdistro.json \
    .config/microsoft-edge/NativeMessagingHosts/qdistro.json; do
    [ -f "$TMP/$sub" ] || { echo "MISS: $sub"; exit 1; }
done
PASS "qdistro-browser-install writes all six per-browser manifests"

# --- bridge stdin/stdout round-trip via subprocess ----
# We spawn the bridge as a subprocess of python3, so the bridge's
# ppid resolves to /usr/bin/python3 (the exec stub bash gets exec'd
# over by python3). We override the allowlist with sys.executable
# so the parent-chain check matches in this driver.
#
# P0-2 (qdistro@0c3a7a8): the legacy QDISTRO_BROWSER_BRIDGE_ALLOWLIST
# now raises so production-env contamination is impossible. The test-
# only override has the _TEST suffix and is gated on
# QDISTRO_TEST_MODE=1.
#
# P0-1 (same commit): the bridge derives extension_id from argv, not
# from the stdio payload. We pass `chrome-extension://probe@s66/` so
# the bridge sees a well-formed Chrome origin.
RESP=$(python3 - "$BRIDGE" <<'PY'
import json, struct, subprocess, sys, os
bridge = sys.argv[1]
env = dict(os.environ)
env.pop("QDISTRO_BROWSER_BRIDGE_ALLOWLIST", None)
env["QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST"] = os.readlink(
    "/proc/self/exe")
env["QDISTRO_TEST_MODE"] = "1"
proc = subprocess.Popen(
    [bridge, "chrome-extension://probe@s66/"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, env=env)
# stdio extension_id is now ignored — bridge trusts argv only.
body = json.dumps({"op": "qdistro.ping", "echo": "s66"}).encode("utf-8")
proc.stdin.write(struct.pack("<I", len(body)) + body)
proc.stdin.flush()
raw_len = proc.stdout.read(4)
if len(raw_len) != 4:
    print("SHORT-FRAME"); sys.exit(2)
n = struct.unpack("<I", raw_len)[0]
resp = proc.stdout.read(n)
proc.stdin.close()
proc.wait(timeout=5)
print(resp.decode())
PY
)
echo "$RESP" | python3 -c "
import json, sys
body = json.loads(sys.stdin.read())
assert body.get('pong') is True, body
assert body.get('op') == 'qdistro.ping', body
assert body.get('echo') == 's66', body
# extension_id MUST be the argv-derived value, not anything stdio.
assert body.get('extension_id') == 'probe@s66', body
# parent_exe resolves to the python interpreter that spawned the
# bridge. Just confirm it ends in 'python3' (the test driver's
# launcher).
assert body.get('parent_exe', '').rsplit('/', 1)[-1].startswith('python3'), body
print('OK')
" >/dev/null
PASS "bridge stdin/stdout round-trip with stubbed parent allowlist"

# --- denial path: real allowlist, parent is /bin/bash NOT in list ----
RESP=$(python3 - "$BRIDGE" <<'PY'
import json, struct, subprocess, sys, os
bridge = sys.argv[1]
env = dict(os.environ)
env.pop("QDISTRO_BROWSER_BRIDGE_ALLOWLIST", None)
env.pop("QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST", None)
env.pop("QDISTRO_TEST_MODE", None)
proc = subprocess.Popen(
    [bridge], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.PIPE, env=env)
body = json.dumps({"op": "qdistro.ping"}).encode("utf-8")
proc.stdin.write(struct.pack("<I", len(body)) + body)
proc.stdin.flush()
raw_len = proc.stdout.read(4)
if len(raw_len) != 4:
    print("SHORT-FRAME"); sys.exit(2)
n = struct.unpack("<I", raw_len)[0]
resp = proc.stdout.read(n)
proc.stdin.close()
proc.wait(timeout=5)
print(resp.decode())
PY
)
echo "$RESP" | python3 -c "
import json, sys
body = json.loads(sys.stdin.read())
assert body.get('ok') is False, body
assert body.get('error') == 'parent_not_allowed', body
print('OK')
" >/dev/null
PASS "bridge denies non-allowlisted parent with clean error"

PASS "§spec/14 Phase-8 MVP browser-bridge in-VM probe"
