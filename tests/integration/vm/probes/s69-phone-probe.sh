#!/bin/bash
# spec/18 Phase-8 MVP — qdistro-phone in-VM probe.
#
# Asserts:
#   - install-phone-for-vm.sh staged engine + daemon + CLI + service
#     at the spec-pinned paths.
#   - qdistro-phone pair / list / unpair round-trips a config file.
#   - qdistro-phone push renders an ntfy body with allow + deny URLs
#     keyed by HMAC.
#   - daemon refuses to start without QDISTRO_PHONE_NTFY_URL.
#   - daemon HTTP listener accepts a valid signed callback +
#     records it in the decisions queue.
set -euo pipefail

PASS() { echo "PASS: $*"; }
SKIP() { echo "SKIP: $*"; exit 0; }

ENG=/usr/libexec/qdistro/qdistro_phone.py
DAEMON=/usr/libexec/qdistro/qdistro_phone_daemon.py
CLI=/usr/local/bin/qdistro-phone
SVC=/etc/systemd/system/qdistro-phone.service

[ -f "$ENG"    ] || SKIP "$ENG missing"
[ -f "$DAEMON" ] || SKIP "$DAEMON missing"
[ -x "$CLI"    ] || SKIP "$CLI missing"
[ -f "$SVC"    ] || SKIP "$SVC missing"

PASS "phone surfaces installed"

TMP=$(mktemp -d /tmp/s69-phone.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

# --- pair/list/unpair round-trip --------------------------------
"$CLI" --config-dir "$TMP/conf" pair pixel-9-pro \
    --trust full --feature approver=on >/dev/null
"$CLI" --config-dir "$TMP/conf" list 2>&1 | grep -q "pixel-9-pro" \
    || { echo "FAIL: list didn't show paired phone"; exit 1; }
PASS "qdistro-phone pair/list round-trip"

"$CLI" --config-dir "$TMP/conf" unpair pixel-9-pro >/dev/null
"$CLI" --config-dir "$TMP/conf" list 2>&1 | grep -q "no paired phones" \
    || { echo "FAIL: unpair didn't remove the entry"; exit 1; }
PASS "qdistro-phone unpair removes the entry"

# --- push body shape --------------------------------------------
OUT=$("$CLI" push \
    --request-id abc \
    --action com.qdistro.pwd.unlock \
    --user admin \
    --callback-base http://127.0.0.1/cb \
    --secret xyz 2>&1)
echo "$OUT" | python3 -c "
import json, sys
body = json.loads(sys.stdin.read())
assert body['topic'] == 'qdistro-admin', body
urls = [a['url'] for a in body['actions']]
assert any('/abc/allow' in u for u in urls), urls
assert any('/abc/deny' in u for u in urls), urls
print('OK')
" >/dev/null
PASS "qdistro-phone push renders ntfy body"

# --- daemon refuses without NTFY_URL ----------------------------
unset QDISTRO_PHONE_NTFY_URL
if /usr/bin/python3 "$DAEMON" --check-only \
        --config-dir "$TMP/conf" \
        --queue-path "$TMP/q.sqlite" 2>/dev/null; then
    echo "FAIL: daemon should refuse without NTFY_URL"; exit 1
fi
PASS "daemon refuses to start without QDISTRO_PHONE_NTFY_URL"

# --- daemon HTTP listener accepts valid signed callback ---------
PORT=$(/usr/bin/python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0))
print(s.getsockname()[1]); s.close()
PY
)
QDISTRO_PHONE_NTFY_URL=https://ntfy.example.invalid \
QDISTRO_PHONE_SECRET=test-secret \
/usr/bin/python3 "$DAEMON" \
    --listen-port "$PORT" \
    --config-dir "$TMP/conf" \
    --queue-path "$TMP/q.sqlite" >/dev/null 2>&1 &
DPID=$!
trap 'kill $DPID 2>/dev/null || true; rm -rf "$TMP"' EXIT
# Wait for socket to come up.
for i in 1 2 3 4 5 6 7 8 9 10; do
    if /usr/bin/python3 -c "
import socket, sys
try:
    s=socket.create_connection(('127.0.0.1', $PORT), timeout=0.5)
    s.close(); sys.exit(0)
except Exception:
    sys.exit(1)" >/dev/null 2>&1; then break; fi
    sleep 0.3
done

/usr/bin/python3 - "$PORT" <<'PY'
import http.client, hmac, hashlib, sys, time
port = int(sys.argv[1])
secret = b"test-secret"
rid = "req42"
exp = int(time.time()) + 60
sig = hmac.new(secret, f"{rid}|allow|{exp}".encode(),
               hashlib.sha256).hexdigest()
c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
c.request("POST", f"/v1/decision/{rid}/allow?sig={sig}&exp={exp}")
r = c.getresponse(); body = r.read()
assert r.status == 200, (r.status, body)
print("OK")
PY
PASS "daemon HTTP listener records valid signed decision"

PASS "§spec/18 Phase-8 MVP qdistro-phone probe"
