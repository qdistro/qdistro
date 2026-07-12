#!/bin/bash
# ===========================================================================
# HOST-SIDE PREREQUISITES (the two-capturable-heads recipe, codex impl-7) — the
# hard part of A1-min. Apply to the VM domain BEFORE running this in-guest:
#
#  1. Build the VM with the test hook compiled in:
#       QDWIN_EXTRA_MESON_OPTS=-Denable_test_place=true scripts/vm/spin-test-vm.sh
#  2. Give the VM ONE virtio-gpu with TWO outputs + egl-headless so QEMU
#     registers a console per scanout (qemu:///session domain XML):
#       <qemu:commandline>
#         <qemu:arg value='-device'/>
#         <qemu:arg value='virtio-gpu-pci,id=gpu0,max_outputs=2,bus=pci.0,addr=0x10'/>
#         <qemu:arg value='-display'/>
#         <qemu:arg value='egl-headless,rendernode=/dev/dri/renderD128'/>
#       </qemu:commandline>
#     (<video><model type='none'/></video>; needs a host render node + EGL.)
#  3. Force the 2nd connector connected (headless QEMU boots it 'disconnected'):
#     append to GRUB_CMDLINE_LINUX_DEFAULT in the guest + regen grub, reboot:
#       video=Virtual-2:800x600e
#  4. Capture each output host-side (virsh --screen only sees head 0; use raw QMP):
#       virsh qemu-monitor-command <vm> \
#         '{"execute":"screendump","arguments":{"filename":"/tmp/out0.ppm","device":"gpu0","head":0}}'
#       virsh qemu-monitor-command <vm> '{...,"head":1}'  -> out1
#     head0 = Virtual-1 = out0 (left, barcode); head1 = Virtual-2 = out1 (right).
#
# Run this script as root in the guest. NOTE: vm-script does NOT forward host
# env, so select calib mode by piping it in: (echo MODE=calib; cat THIS) | vm-script <vm>
# ===========================================================================
# A1-min straddle via the PRODUCTION compositor unit (noctalia-session.service),
# which already acquires seat0 correctly (admin user systemd, active session,
# seatd). We override it to drive TWO adjacent DRM outputs (two virtio-gpu cards)
# at WxH/scale1/pixman, inject the QDWIN_TEST_PLACE_* straddle hook env, and stop
# the QML shell to minimise chrome. Run as root in the VM.
#
# Env: MODE=straddle|calib  W H MW MH SEAM GEN OID  (defaults below)
set -uo pipefail
MODE=${MODE:-straddle}
W=${W:-800}; H=${H:-600}
MW=${MW:-512}; MH=${MH:-400}; SEAM=${SEAM:-256}
GEN=${GEN:-20}; OID=${OID:-1}
MY=100
if [ "$MODE" = calib ]; then MX=100; else MX=$((W - SEAM)); fi

A() { runuser -l admin -c "XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus $*"; }

# Two-output pixman weston.ini for qdwin.
A "cp -n ~/weston.ini ~/weston.ini.a1bak 2>/dev/null; cat > ~/weston.ini <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
renderer=pixman
idle-time=0

[shell]
locking=false

[output]
name=Virtual-1
mode=${W}x${H}
scale=1

[output]
name=Virtual-2
mode=${W}x${H}
scale=1

[pipewire]
num-outputs=2
EOF"

# Drop-in: inject the straddle placement hook env into the compositor unit.
A "mkdir -p ~/.config/systemd/user/noctalia-session.service.d; cat > ~/.config/systemd/user/noctalia-session.service.d/a1-testplace.conf <<EOF
[Service]
Environment=QDWIN_TEST_PLACE_APPID=qdwin-marker-client
Environment=QDWIN_TEST_PLACE_X=${MX}
Environment=QDWIN_TEST_PLACE_Y=${MY}
EOF"

# Stop the QML shell (chrome) + restart the compositor with the new config/env.
A "systemctl --user stop noctalia-shell.service 2>/dev/null || true"
A "systemctl --user daemon-reload"
A "systemctl --user stop mm-marker-a1.service 2>/dev/null || true; pkill -u admin -f qdwin-marker-client 2>/dev/null || true"
A "systemctl --user restart noctalia-session.service"
sleep 4
if ! runuser -l admin -c '[ -S /run/user/1000/wayland-1 ]'; then
  echo "FAIL: wayland-1 missing after restart"
  A "systemctl --user status noctalia-session.service --no-pager | tail -20"
  exit 7
fi
echo "qdwin up (wayland-1)"

echo "--- qdwin output geometry (journal) ---"
A "journalctl --user -u noctalia-session.service --no-pager | grep -iE 'Output |Virtual|associating|connector|head ' | tail -30"

# Launch the marker as a normal toplevel into wayland-1; hook places it.
A "systemd-run --user --collect --unit=mm-marker-a1 --setenv=WAYLAND_DISPLAY=wayland-1 \
   qdwin-marker-client --width $MW --height $MH --seam-x $SEAM \
   --output-id $OID --generation $GEN --frame 0 --animate-ms 200"
sleep 3
echo "--- placement (journal) ---"
A "journalctl --user -u noctalia-session.service --no-pager | grep -iE 'TEST placement|marker' | tail -5"
echo "A1_SETUP_OK MODE=$MODE MX=$MX MY=$MY W=$W H=$H MW=$MW MH=$MH SEAM=$SEAM GEN=$GEN"
