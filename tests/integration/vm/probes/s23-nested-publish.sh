#!/bin/bash
# §6.8 S1 — nested-side per-toplevel PipeWire publish + advertise.
#
# This is the real S1 acceptance: a *nested* weston instance loads
# qdwin-shell.so in publisher mode (QDWIN_NESTED_MODE=1), wires its
# inner toplevels through dynamically-created pipewire outputs, and
# advertises each one to the outer qdwin via qdwin_nested_manager_v1.
#
# Probe sequence:
#   1. Start outer qdwin with rdp + pipewire backends.
#   2. Connect a sdl-freerdp peer so outer paints + nested
#      backend-wayland has something to attach to.
#   3. Start nested weston:
#        - backend=wayland-backend.so,pipewire-backend.so
#        - shell=qdwin-shell.so + QDWIN_NESTED_MODE=1
#        - QDWIN_OUTER_DISPLAY=wayland-1
#   4. Spawn weston-terminal inside the nested weston.
#   5. Verify outer log contains:
#        - "qdwin: nested_manager bound v2" (the publisher binding back)
#        - "qdwin: nested-toplevel advertise pw_node='weston.pipewire:<pid>:..."
#      and nested log contains:
#        - "qdwin/nested: advertised handle=..."
#
# Exit codes:
#   0 — PASS
#   2 — outer qdwin failed to start
#   3 — nested weston failed to start
#   4 — publisher never bound the manager on the outer
#   5 — no advertise_toplevel log line on the outer
#   6 — nested side failed to log advertised
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s23-weston-outer.log
NLOG=/home/admin/s23-weston-nested.log
INI=/home/admin/.config/weston.ini
NESTED_INI=/home/admin/.config/weston-nested-pub.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
loginctl enable-linger admin 2>/dev/null || true

pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
pkill -9 -f "weston-terminal" 2>/dev/null || true
sleep 1

# --- stage qdshell + protocol XMLs ----------------------------------
rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-nested-v1.xml" \
    /home/admin/qdshell/qdwin-nested-v1.xml
chown -R admin:admin /home/admin/qdshell
runuser -u admin -- env QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    /home/admin/qdshell/gen_protocol.sh >/dev/null

install -d -o admin -g admin /home/admin/.config

# --- outer weston.ini -----------------------------------------------
cat >"$INI" <<EOF
[core]
shell=qdwin-shell.so
backend=rdp-backend.so,pipewire-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=rdp-0
mode=1280x720

[pipewire]
num-outputs=1
EOF
chown admin:admin "$INI"

# --- nested weston.ini -- publisher mode ----------------------------
# Pre-allocate a small pool of pipewire outputs; nested-mode qdwin-shell
# pins one per inner toplevel round-robin. backend-pipewire's
# weston_pipewire_output_api_v2 isn't reachable from a shell plugin
# (head_create takes a weston_backend* not a weston_compositor*), so
# static pool > dynamic create here.
cat >"$NESTED_INI" <<EOF
[core]
shell=qdwin-shell.so
backend=wayland-backend.so,pipewire-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=WL1
mode=800x600

[pipewire]
num-outputs=8
EOF
chown admin:admin "$NESTED_INI"

rm -f "$WLOG" "$NLOG"; touch "$WLOG" "$NLOG"; chown admin:admin "$WLOG" "$NLOG"

# --- start outer qdwin ----------------------------------------------
cat >/home/admin/run-s23-outer.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$INI \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s23-outer.sh; chown admin:admin /home/admin/run-s23-outer.sh
runuser -u admin -- nohup /home/admin/run-s23-outer.sh >>"$WLOG" 2>&1 </dev/null &
OUTPID=$!

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done
grep -q 'qdwin: shell loaded' "$WLOG" || {
    echo "FAIL: outer weston did not load qdwin-shell"
    tail -20 "$WLOG"
    exit 2
}
echo "PASS: outer qdwin started"
chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# --- attach a peer so outer paints ----------------------------------
runuser -u admin -- env SDL_VIDEODRIVER=dummy \
    nohup timeout 90 sdl-freerdp /v:127.0.0.1:3389 \
        /cert:ignore /u:probe /p:probe \
        >/tmp/s23-sdl-freerdp.log 2>&1 </dev/null &
SDLPID=$!
for i in 1 2 3 4 5 6 7 8; do
    grep -q "seat '" "$WLOG" 2>/dev/null && break
    sleep 1
done

# --- start nested weston (publisher) --------------------------------
cat >/home/admin/run-s23-nested.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-1
export QDWIN_NESTED_MODE=1
export QDWIN_OUTER_DISPLAY=wayland-1
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --config=$NESTED_INI \\
    -Sqdwin-nested-pub \\
    --log=$NLOG
EOF
chmod +x /home/admin/run-s23-nested.sh; chown admin:admin /home/admin/run-s23-nested.sh
runuser -u admin -- nohup /home/admin/run-s23-nested.sh >>"$NLOG" 2>&1 </dev/null &
NESTEDPID=$!

# Nested takes a few seconds to load both backends + bind outer manager.
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 1
    grep -q 'nested-mode publisher ready' "$NLOG" 2>/dev/null && break
done

if ! grep -q 'nested-mode publisher ready' "$NLOG"; then
    echo "FAIL: nested weston did not initialise publisher mode"
    echo "--- nested log ---"
    tail -40 "$NLOG"
    pkill -9 -f run-s23-nested 2>/dev/null || true
    pkill -9 -x weston 2>/dev/null || true
    exit 3
fi
echo "PASS: nested weston publisher mode ready"

if ! grep -q 'nested_manager bound' "$WLOG"; then
    echo "FAIL: outer never logged 'nested_manager bound'"
    echo "--- outer log tail ---"
    tail -30 "$WLOG"
    pkill -9 -f run-s23-nested 2>/dev/null || true
    pkill -9 -x weston 2>/dev/null || true
    exit 4
fi
echo "PASS: publisher bound qdwin_nested_manager_v1 on outer"

# --- spawn an inner client (weston-terminal) ------------------------
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=qdwin-nested-pub \
    nohup weston-terminal >/tmp/s23-wt.log 2>&1 </dev/null &
WTPID=$!

# Wait for advertise.
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 1
    grep -q 'nested-toplevel advertise' "$WLOG" 2>/dev/null && break
done

if ! grep -q 'nested-toplevel advertise' "$WLOG"; then
    echo "FAIL: outer never logged 'nested-toplevel advertise'"
    echo "--- outer log tail ---"
    tail -30 "$WLOG"
    echo "--- nested log tail ---"
    tail -30 "$NLOG"
    kill "$WTPID" 2>/dev/null || true
    pkill -9 -f run-s23-nested 2>/dev/null || true
    pkill -9 -x weston 2>/dev/null || true
    exit 5
fi
echo "PASS: outer received advertise_toplevel from publisher"

if ! grep -q "qdwin/nested: advertised handle=" "$NLOG"; then
    echo "FAIL: nested side never logged 'advertised handle=...'"
    echo "--- nested log tail ---"
    tail -30 "$NLOG"
    exit 6
fi
echo "PASS: nested publisher logged the advertise"

# Confirm pw_node string is the expected shape.
ADV_LINE=$(grep 'nested-toplevel advertise' "$WLOG" | head -1)
echo "advertise line: $ADV_LINE"
if echo "$ADV_LINE" | grep -q "pw_node='weston.pipewire:"; then
    echo "PASS: pw_node carries weston.pipewire:<pid>:<output-name>"
else
    echo "FAIL: pw_node format unexpected"
    exit 7
fi

# §6.8 S2: outer should have synthesised a proxy toplevel + curtain.
PROXY_LINE=$(grep 'qdwin/nested-proxy: created' "$WLOG" | head -1)
echo "proxy line: $PROXY_LINE"
if echo "$PROXY_LINE" | grep -q "qdwin/nested-proxy: created handle="; then
    echo "PASS: §6.8 S2 outer proxy toplevel + curtain created"
else
    echo "FAIL: §6.8 S2 proxy creation not logged"
    exit 8
fi

# §6.8 S3: outer should have connected to the input sink and sent
# PING; nested should have logged the connect + PING.
if grep -q 'input-sink PING sent' "$WLOG"; then
    echo "PASS: §6.8 S3 outer connected to input sink + sent PING"
else
    echo "FAIL: outer did not log input-sink PING send"
    exit 9
fi
if grep -q 'input-sink PING' "$NLOG" && \
   grep -q 'wire-format proven' "$NLOG"; then
    echo "PASS: §6.8 S3 nested received PING — wire format proven"
else
    echo "FAIL: nested did not log PING receive"
    exit 10
fi

# --- teardown -------------------------------------------------------
kill "$WTPID" 2>/dev/null || true
pkill -9 -f weston-terminal 2>/dev/null || true

# Verify nested cleanup logs unpublish.
sleep 2
if grep -q 'qdwin/nested: unpublish handle=' "$NLOG"; then
    echo "PASS: nested publisher unpublished on inner-client teardown"
else
    echo "WARN: did not see unpublish line (client may have outlived sleep)"
fi

# §6.8 S2: outer should have torn down the proxy toplevel + curtain.
if grep -q 'qdwin/nested-proxy: destroy handle=' "$WLOG"; then
    echo "PASS: §6.8 S2 outer proxy torn down on resource destroy"
else
    echo "WARN: did not see proxy destroy (resource may not have closed)"
fi

kill "$NESTEDPID" 2>/dev/null || true
pkill -9 -f run-s23-nested 2>/dev/null || true
kill "$SDLPID" 2>/dev/null || true
pkill -9 -f "sdl-freerdp.*:3389" 2>/dev/null || true
kill "$OUTPID" 2>/dev/null || true
pkill -9 -x weston 2>/dev/null || true

echo
echo "PASS: §6.8 S1 nested-side publish + advertise end-to-end"
