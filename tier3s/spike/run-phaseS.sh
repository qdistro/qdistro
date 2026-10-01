#!/bin/bash
# tier3s/spike/run-phaseS.sh — HOST side driver for the whole Phase S spike.
#   run-phaseS.sh <vm> <logdir> <src-url>
# Every build/podman/runsc step runs INSIDE the VM through vmlog.sh (vm-exec);
# the host only stages source, takes virsh screenshots and sends QMP keys.
# Commit before launching; never edit while it runs.
set -uo pipefail
vm=$1 L=$2 SRC=$3
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VMLOG=$here/vmlog.sh
GUI=$repo/scripts/vm/vm-gui
[ -e "$L/00-stage-src.log" ] && { echo "refusing: $L already has a run (vmlog appends)"; exit 2; }
mkdir -p "$L/screens"
shot() { timeout 120 "$GUI" "$vm" screenshot-fresh "$L/screens/$1" >/dev/null 2>&1; echo "screenshot $L/screens/$1"; }
R='cd /root/qdistro-src &&'
# qdlocker idle-locks after 5 min; the last qdwin locked_changed= line is the truth.
lock_state() { timeout 60 "$repo/scripts/vm/vm-exec" "$vm" "journalctl _UID=1000 --no-pager -o cat | grep -o 'locked_changed=[01]' | tail -1" 2>/dev/null | grep -o 'locked_changed=[01]' | tail -1; }
ensure_unlocked() {
  local st; st=$(lock_state)
  [ "$st" = locked_changed=1 ] && "$here/host-unlock.sh" "$vm"
  st=$(lock_state); echo "lock state before GUI step: ${st:-unknown}"
  [ "$st" != locked_changed=1 ] || { echo "ABORT: session still locked"; exit 3; }
}

echo "== 00 stage source (git archive of HEAD $(git -C "$repo" rev-parse --short HEAD))"
VMLOG_TAIL=0 "$VMLOG" "$L/00-stage-src.log" "$vm" "mkdir -p /root/qdistro-src && cd /root/qdistro-src && curl -fsS $SRC | tar -xf - && sha256sum tier3s/spike/*.sh tier3s/spike/smoke.json tier3s/tier3s-runsc tier3s/RUNSC_RELEASE && /usr/libexec/qdistro/runsc/runsc --version && cat /etc/qdistro/profile && tier3s/probe.sh --user admin; python3 tier3s/spike/make-smoke-json.py --check && echo smoke.json-matches-generator"
echo "== 01 stage image"
VMLOG_TAIL=0 VMLOG_TIMEOUT=1500 "$VMLOG" "$L/01-stage-image.log" "$vm" "$R tier3s/spike/stage-image.sh /var/tmp/tier3s-rpms"
echo "== 10 s1 headless hello"
VMLOG_TAIL=0 "$VMLOG" "$L/10-s1-headless-hello.log" "$vm" "$R tier3s/spike/s1-headless-hello.sh"
"$here/vmfetch.sh" "$vm" /var/tmp/tier3s-spike/s1 "$L/s1-artifacts"
echo "== 20/21 s2 wayland-info + negative"
VMLOG_TAIL=0 "$VMLOG" "$L/20-s2-wayland-info.log" "$vm" "$R tier3s/spike/s2-waypipe.sh info; cat /var/tmp/tier3s-spike/s2-info/wayland-info.txt"
VMLOG_TAIL=0 "$VMLOG" "$L/21-s2-negative-no-host-uds.log" "$vm" "$R tier3s/spike/s2-waypipe.sh info --no-host-uds"
ensure_unlocked; sleep 1; shot 19-desktop-before.png
for app in weston-terminal foot; do
  n=$([ $app = weston-terminal ] && echo 22 || echo 23)
  echo "== $n s2 $app"
  ensure_unlocked
  VMLOG_TAIL=0 "$VMLOG" "$L/$n-s2-$app.log" "$vm" "$R tier3s/spike/s2-waypipe.sh start $app"
  sleep 3; shot "$n-$app.png"
  timeout 60 "$GUI" "$vm" click 960 560 >/dev/null 2>&1
  timeout 120 "$GUI" "$vm" type "echo typed-into-$app; uname -r; cat /proc/self/cgroup; ls /dev/dri" >/dev/null 2>&1
  timeout 30 "$GUI" "$vm" key Return >/dev/null 2>&1; sleep 2
  shot "$n-$app-input.png"
  VMLOG_TAIL=0 "$VMLOG" "$L/$n-s2-$app.log" "$vm" "$R tier3s/spike/s2-waypipe.sh status $app"
  VMLOG_TAIL=0 "$VMLOG" "$L/$n-s2-$app.log" "$vm" "$R tier3s/spike/s2-waypipe.sh stop $app"
  sleep 1; shot "$n-$app-after-stop.png"
  "$here/vmfetch.sh" "$vm" /var/tmp/tier3s-spike/s2-$app "$L/s2-$app-artifacts"
done
"$here/vmfetch.sh" "$vm" /var/tmp/tier3s-spike/s2-info "$L/s2-info-artifacts"
echo "== 30-33 s3 cgroup placement"
i=30
for shape in root-unit parent-root parent-user split; do
  VMLOG_TAIL=0 "$VMLOG" "$L/$i-s3-$shape.log" "$vm" "$R tier3s/spike/s3-cgroups.sh $shape"
  grep -h VERDICT "$L/$i-s3-$shape.log"; i=$((i+1))
done
echo "== 40 final state"
VMLOG_TAIL=0 "$VMLOG" "$L/40-final-state.log" "$vm" "$R . tier3s/spike/lib.sh; as_admin podman ps -a; for p in /proc/[0-9]*; do e=\$(readlink \$p/exe 2>/dev/null) || continue; case \$e in /usr/libexec/qdistro/runsc/*|/usr/bin/waypipe|/usr/bin/conmon) echo LEFTOVER \${p#/proc/} \$e;; esac; done; echo '(end leftovers)'; systemctl list-units 't3s-*' --all --no-legend; echo '(end units)'"
echo "RUN-PHASES DONE"
