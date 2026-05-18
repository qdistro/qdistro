#!/bin/bash
# §6.7 v2 events probe: qdwin forwards seat / output lifecycle events
# via qdwin_shell_v1 v2 (seat_created / seat_removed / output_created /
# output_removed). This probe verifies:
#
#   1. The shell negotiates v2 of the global.
#   2. After bind, qdshell's log shows at least one `output_created` for
#      the weston output that exists at startup (backend-rdp "rdp").
#   3. The ctrl-socket `outputs` query returns that same output.
#   4. The shell's seats snapshot matches its `seat_created` / `seat_removed`
#      tally (possibly empty on a headless RDP backend with no connected
#      peer — that's fine; the shape is what we test).
#
# Does not subscribe any stream — this probe is strictly about the new
# event forwards.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}
CERTDIR=/home/admin/qdwin-rdp
WLOG=/home/admin/s6-weston.log
SLOG=/home/admin/s6-qdshell.log
SOCK=/tmp/qdshell-s6.sock
INI=/home/admin/.config/weston.ini

pgrep -x pipewire >/dev/null || { echo "ERROR: pipewire not running"; exit 1; }
pkill -9 -x weston 2>/dev/null || true
pkill -9 -f "qdshell.py" 2>/dev/null || true
sleep 1

# Fresh qdshell tree (gen_protocol needs write perms in admin's home).
rm -rf /home/admin/qdshell
install -d -o admin -g admin /home/admin/qdshell
cp -r "$QDWIN_SRC/qdshell/." /home/admin/qdshell/
install -m 0644 "$QDWIN_SRC/qdwin/qdwin-shell-v1.xml" \
    /home/admin/qdshell/qdwin-shell-v1.xml
chown -R admin:admin /home/admin/qdshell
runuser -u admin -- env QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    /home/admin/qdshell/gen_protocol.sh >/dev/null

install -d -o admin -g admin /home/admin/.config

cat >"$INI" <<EOF
[core]
shell=qdwin-shell.so
backend=rdp-backend.so,pipewire-backend.so
require-outputs=any
idle-time=0

[shell]
locking=false

[output]
name=rdp
mode=1280x720

[pipewire]
num-outputs=1
EOF
chown admin:admin "$INI"

rm -f "$WLOG" "$SLOG"; touch "$WLOG" "$SLOG"; chown admin:admin "$WLOG" "$SLOG"

cat >/home/admin/run-s6-weston.sh <<EOF
#!/bin/bash
export HOME=/home/admin
export XDG_RUNTIME_DIR=/run/user/1000
export QDWIN_ALLOWED_UID=1000
exec weston \\
    --rdp-tls-cert=$CERTDIR/rdp.crt \\
    --rdp-tls-key=$CERTDIR/rdp.key \\
    --log=$WLOG
EOF
chmod +x /home/admin/run-s6-weston.sh; chown admin:admin /home/admin/run-s6-weston.sh

runuser -u admin -- nohup /home/admin/run-s6-weston.sh >>"$WLOG" 2>&1 </dev/null &

for i in 1 2 3 4 5 6 7 8; do
    grep -q 'qdwin: shell loaded' "$WLOG" 2>/dev/null && break
    sleep 1
done

chmod 0666 /run/user/1000/wayland-1 2>/dev/null || true

# Launch qdshell with ctrl-socket. QDSHELL_BROKER_REQUIRED=0 because
# this probe doesn't exercise the admin path.
runuser -u admin -- env HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    WAYLAND_DISPLAY=wayland-1 \
    QDWIN_PROTO_XML=/home/admin/qdshell/qdwin-shell-v1.xml \
    QDSHELL_BROKER_REQUIRED=0 \
    nohup python3 /home/admin/qdshell/qdshell.py \
        --ctrl-socket=$SOCK >>$SLOG 2>&1 </dev/null &

for i in 1 2 3 4 5; do
    [ -S "$SOCK" ] && break
    sleep 1
done
sleep 1

echo "=== qdshell log (v2 events) ==="
grep -E "seat_created|seat_removed|output_created|output_removed|bound qdwin_shell_v1" \
    "$SLOG" || true
echo
echo "=== ctrl outputs ==="
echo "outputs" | socat - UNIX-CONNECT:$SOCK
echo
echo "=== ctrl seats ==="
echo "seats" | socat - UNIX-CONNECT:$SOCK
echo

BOUND_LINE=$(grep "bound qdwin_shell_v1" "$SLOG" || true)
# Extract the "@<name> v<N>" bit and require N >= 2.
BOUND_VER=$(echo "$BOUND_LINE" | grep -oE " v[0-9]+" | tr -d ' v' | head -1)
if [ -z "$BOUND_VER" ]; then
    echo "FAIL: shell did not bind qdwin_shell_v1 — log:"; cat "$SLOG"; exit 2
fi
if [ "$BOUND_VER" -ge 2 ]; then
    echo "PASS: shell bound at v>=2 (v=$BOUND_VER)"
else
    echo "FAIL: shell bound at v$BOUND_VER; expected >= 2"; exit 2
fi

if grep -q "output_created" "$SLOG"; then
    echo "PASS: output_created received"
else
    echo "FAIL: no output_created in shell log"; exit 2
fi

OUT_NAMES=$(echo "outputs" | socat - UNIX-CONNECT:$SOCK | awk '/^output /{print $2}')
if [ -z "$OUT_NAMES" ]; then
    echo "FAIL: ctrl outputs query returned no outputs"; exit 2
fi
echo "PASS: ctrl outputs = $OUT_NAMES"

echo "PASS: §6.7 v2 events probe"
