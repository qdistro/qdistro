#!/bin/bash
# R2 viewer: production qdistro-mm broker/wrappers plus real qdshell on qdwin.
set -uo pipefail

RT=/run/mm-vb
SOCK=wayland-vb
RECEIPT=${PAIRING_RECEIPT:?need PAIRING_RECEIPT}
STREAMS=${STREAM_SESSION:?need STREAM_SESSION}
VIEWER_ID=${VIEWER_MACHINE_ID:?need VIEWER_MACHINE_ID}
QDSHELL=${QDSHELL_PATH:-/tmp/qdshell-r2}
MMROOT=${MMROOT:-/tmp/mm}
MMBIN=$MMROOT/multimachine
MM=/usr/lib64/libweston-16
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"

for command in weston qs qdistro-secctx-exec sdl-freerdp busctl; do
  command -v "$command" >/dev/null || { echo "FAIL: missing $command"; exit 5; }
done
for program in qdistro-mm-session-launcher qdistro-mm-broker \
               qdistro-mm-rdp-client-wrapper; do
  [ -x "$MMBIN/$program" ] || { echo "FAIL: missing $MMBIN/$program"; exit 5; }
done
[ -f "$QDSHELL/shell.qml" ] || { echo "FAIL: qdshell source missing"; exit 5; }

systemctl stop mm-viewer-session mm-qdwin mm-seatd mm-ydotoold 2>/dev/null || true
systemctl reset-failed mm-viewer-session mm-qdwin mm-seatd mm-ydotoold 2>/dev/null || true
systemctl stop greetd-qdwin greetd qdistro-session-manager seatd.service seatd.socket 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell qdlocker qdshell.service 2>/dev/null || true
pkill -x seatd 2>/dev/null || true
pkill -f 'sdl-freerdp|/usr/bin/qs -p /tmp/qdshell-r2' 2>/dev/null || true
rm -f /run/seatd.sock /run/.ydotool_socket 2>/dev/null || true

systemd-run --collect --unit=mm-seatd seatd
for _ in $(seq 1 40); do [ -S /run/seatd.sock ] && break; sleep 0.2; done
[ -S /run/seatd.sock ] || { echo "FAIL: seatd socket missing"; exit 6; }
systemd-run --collect --unit=mm-ydotoold \
  ydotoold --socket-path=/run/.ydotool_socket --socket-perm=0666
for _ in $(seq 1 40); do [ -S /run/.ydotool_socket ] && break; sleep 0.2; done

rm -rf "$RT"; mkdir -p "$RT"; chmod 0700 "$RT"
cat > "$RT/qdwin.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
idle-time=0
[shell]
locking=false
EOF

systemd-run --collect --unit=mm-qdwin \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=LIBSEAT_BACKEND=seatd --setenv="WESTON_MODULE_MAP=$WMAP" \
  --setenv=QDWIN_ALLOWED_UID=0 --setenv=QDWIN_ALLOWED_LOCKER_ANY=1 \
  --setenv=QDWIN_SECCTX_OPEN=1 \
  weston --backend=drm-backend.so --renderer=pixman \
    --config="$RT/qdwin.ini" --socket=$SOCK
for _ in $(seq 1 80); do [ -S "$RT/$SOCK" ] && break; sleep 0.2; done
[ -S "$RT/$SOCK" ] || { echo "FAIL: qdwin socket missing"; exit 7; }

cat > /tmp/mm-viewer-session-inner.sh <<'EOF'
#!/bin/bash
set -uo pipefail
receipt=$1 streams=$2 viewer=$3 qdshell=$4 mmbin=$5
export PATH="$mmbin:$PATH"
exec 3<"$receipt"
exec 4<"$streams"
rm -f "$receipt" "$streams"
/usr/bin/qs -p "$qdshell" --no-color -vv > /run/mm-vb/qdshell.log 2>&1 &
shell=$!
printf '%s\n' "$shell" > /run/mm-vb/qdshell.pid
chmod 0600 /run/mm-vb/qdshell.pid
exec 5</run/mm-vb/qdshell.pid
rm -f /run/mm-vb/qdshell.pid
"$mmbin/qdistro-mm-session-launcher" \
  --pairing-fd 3 --streams-fd 4 --shell-pid-fd 5 \
  --viewer-machine-id "$viewer" \
  --broker-program "$mmbin/qdistro-mm-broker" \
  > /run/mm-vb/broker.log 2>&1 &
broker=$!
for _ in $(seq 1 80); do
  busctl --user --no-pager status org.qdistro.MultiMachine1 >/dev/null 2>&1 && break
  kill -0 "$broker" 2>/dev/null || { cat /run/mm-vb/broker.log; exit 8; }
  sleep 0.25
done
busctl --user --no-pager status org.qdistro.MultiMachine1 >/dev/null 2>&1 \
  || { echo "broker did not claim D-Bus"; cat /run/mm-vb/broker.log; exit 8; }
wait "$shell"
rc=$?
kill "$broker" 2>/dev/null || true
wait "$broker" 2>/dev/null || true
exit "$rc"
EOF
chmod 0755 /tmp/mm-viewer-session-inner.sh

systemd-run --collect --unit=mm-viewer-session \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=WAYLAND_DISPLAY=$SOCK \
  --setenv=XDG_SESSION_TYPE=wayland --setenv=HOME=/root \
  --setenv=QML_DISABLE_DISK_CACHE=1 \
  --setenv=QML_IMPORT_PATH=/usr/share/qdistro/qml \
  --setenv=PYTHONPATH=$MMROOT \
  --setenv=QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
  dbus-run-session -- /tmp/mm-viewer-session-inner.sh \
    "$RECEIPT" "$STREAMS" "$VIEWER_ID" "$QDSHELL" "$MMBIN"

READY=0
for _ in $(seq 1 160); do
  vouched=$(grep -c '\[mm\] broker-vouched origin=' "$RT/qdshell.log" 2>/dev/null || true)
  vouched=${vouched:-0}
  if [ "$vouched" -ge 2 ]; then READY=1; break; fi
  systemctl is-active mm-viewer-session >/dev/null 2>&1 \
    || { echo "FAIL: viewer session exited"; break; }
  sleep 0.5
done
echo "--- broker ---"; tail -30 "$RT/broker.log" 2>/dev/null || true
echo "--- qdshell mm ---"; grep '\[mm\]' "$RT/qdshell.log" 2>/dev/null || true
[ "$READY" = 1 ] || { echo "FAIL: two broker-vouched windows not ready"; exit 9; }
echo "VMB_QDSHELL_OK runtime=$RT socket=$SOCK"
