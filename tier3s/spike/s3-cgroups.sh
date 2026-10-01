#!/bin/bash
# tier3s/spike/s3-cgroups.sh — Phase S step 3 (cgroup placement), as root
# INSIDE the dev VM, sandbox steps as admin. THROWAWAY.
#
#   s3-cgroups.sh <shape>
#     root-unit   03 step 3 as written: root transient unit with MemoryMax/
#                 TasksMax -> runuser -u admin -> podman (default --cgroups)
#     parent-root (a) a transient scope created and owned by the root unit,
#                 delegated to admin; podman --cgroup-manager=cgroupfs
#                 --cgroup-parent=<that scope's cgroup>
#     parent-user (a') systemd cgroup manager: --cgroup-parent=t3sspike.slice (user manager)
#                 (rootless podman + systemd manager only accepts a slice)
#     scope-plain control for (a): the same root scope with MemoryMax/TasksMax
#                 but NO Delegate, NO chown and NO --cgroup-parent; cgroupfs
#                 manager. Shows whether (a)'s containment comes from the
#                 parent flag or simply from nobody moving the processes.
#     split       (b) runuser -u admin -> systemd-run --user --scope
#                 -p Delegate=yes -> podman run --cgroups=split
# (a) and (b) are never combined (podman-run(1): split excludes --cgroup-parent).
# For each shape: the sandbox runs `sleep 45`; we read the target cgroup's
# cgroup.procs RECURSIVELY, then classify every process of the sandbox
# (conmon, runsc gofer, gvisor_sentry, stubs, fd-parking) as INSIDE/OUTSIDE.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
shape=${1:?usage: s3-cgroups.sh <root-unit|parent-root|scope-plain|parent-user|split>}
NAME=t3s-s3-$shape
U=t3s-s3-$shape
OUT=$WORK/s3-$shape; rm -rf "$OUT"; mkdir -p "$OUT"; chown $ADMIN: "$OUT"
ENVA=(env -i PATH=/usr/bin:/bin HOME=/home/$ADMIN USER=$ADMIN LOGNAME=$ADMIN
      XDG_RUNTIME_DIR=$ADMIN_RT DBUS_SESSION_BUS_ADDRESS=unix:path=$ADMIN_RT/bus)
t3s_global; G=("${T3S_GLOBAL[@]}")
as_admin "${G[@]}" rm -f "$NAME" >/dev/null 2>&1
systemctl stop "$U.service" "$U.scope" 2>/dev/null; systemctl reset-failed "$U.service" "$U.scope" 2>/dev/null

T3S_RTFLAGS=(); T3S_RUNOPTS=(--name "$NAME"); EXTRA_GLOBAL=()
case $shape in
  parent-user) T3S_RUNOPTS+=(--cgroup-parent=t3sspike.slice) ;;
  split)       T3S_RUNOPTS+=(--cgroups=split) ;;
esac

launch() {
  case $shape in
  root-unit)
    t3s_podman_argv; ARGV=("${T3S_ARGV[@]}" "$IMG" sleep 45)
    printf 'unit argv: systemd-run --unit=%s -p MemoryMax=1G -p TasksMax=512 -- runuser -u admin -- env -i … ' "$U"; printf ' %q' "${ARGV[@]}"; echo
    systemd-run --quiet --unit="$U" --collect -p MemoryMax=1G -p TasksMax=512 \
        -p StandardOutput=file:"$OUT/run.log" -p StandardError=file:"$OUT/run.log" -- \
        runuser -u $ADMIN -- "${ENVA[@]}" "${ARGV[@]}"
    TARGET=/system.slice/$U.service ;;
  parent-root)
    # Root creates the scope (MemoryMax/TasksMax/Delegate) and hands its cgroup
    # to admin the way systemd delegation does (chown dir + procs/subtree files);
    # the scope's first process then drops to admin and runs podman with the
    # cgroupfs manager, parent = a child cgroup of that scope (the scope cgroup
    # itself holds runuser/podman, and cgroup v2 forbids controllers on a
    # cgroup with member processes).
    t3s_global; EXTRA_GLOBAL=(--cgroup-manager=cgroupfs)
    T3S_RUNOPTS+=(--cgroup-parent=/system.slice/$U.scope/sandbox)
    t3s_podman_argv
    ARGV=("${T3S_ARGV[@]:0:1}" "${EXTRA_GLOBAL[@]}" "${T3S_ARGV[@]:1}" "$IMG" sleep 45)
    printf 'scope argv: systemd-run --scope --unit=%s -p Delegate=yes -p MemoryMax=1G -p TasksMax=512 -- sh -c <chown own cgroup to admin; mkdir sandbox> runuser -u admin -- env -i … ' "$U"; printf ' %q' "${ARGV[@]}"; echo
    cat > "$OUT/inner.sh" <<IN
#!/bin/sh
cg=/sys/fs/cgroup\$(sed 's/^0:://' /proc/self/cgroup)
mkdir -p "\$cg/sandbox"
chown $ADMIN: "\$cg" "\$cg/cgroup.procs" "\$cg/cgroup.subtree_control" "\$cg/cgroup.threads" "\$cg/sandbox" "\$cg/sandbox/cgroup.procs" "\$cg/sandbox/cgroup.subtree_control" "\$cg/sandbox/cgroup.threads"
echo "scope cgroup: \$cg controllers=\$(cat \$cg/cgroup.controllers)"
exec "\$@"
IN
    chmod 0755 "$OUT/inner.sh"
    setsid systemd-run --quiet --scope --unit="$U" -p Delegate=yes -p MemoryMax=1G -p TasksMax=512 -- \
        "$OUT/inner.sh" runuser -u $ADMIN -- "${ENVA[@]}" "${ARGV[@]}" > "$OUT/run.log" 2>&1 < /dev/null &
    TARGET=/system.slice/$U.scope ;;
  scope-plain)
    t3s_global; EXTRA_GLOBAL=(--cgroup-manager=cgroupfs)
    t3s_podman_argv
    ARGV=("${T3S_ARGV[@]:0:1}" "${EXTRA_GLOBAL[@]}" "${T3S_ARGV[@]:1}" "$IMG" sleep 45)
    printf 'scope argv: systemd-run --scope --unit=%s -p MemoryMax=1G -p TasksMax=512 -- runuser -u admin -- env -i … ' "$U"; printf ' %q' "${ARGV[@]}"; echo
    setsid systemd-run --quiet --scope --unit="$U" -p MemoryMax=1G -p TasksMax=512 -- \
        runuser -u $ADMIN -- "${ENVA[@]}" "${ARGV[@]}" > "$OUT/run.log" 2>&1 < /dev/null &
    TARGET=/system.slice/$U.scope ;;
  parent-user)
    t3s_podman_argv; ARGV=("${T3S_ARGV[@]}" "$IMG" sleep 45)
    printf 'argv (as admin, run -d):'; printf ' %q' "${ARGV[@]}"; echo
    T3S_RUNOPTS+=(-d); t3s_podman_argv; ARGV=("${T3S_ARGV[@]}" "$IMG" sleep 45)
    as_admin "${ARGV[@]}" > "$OUT/run.log" 2>&1
    TARGET=/user.slice/user-$ADMIN_UID.slice/user@$ADMIN_UID.service/t3sspike.slice ;;  # no dash: systemd nests a-b.slice under a.slice
  split)
    t3s_podman_argv; ARGV=("${T3S_ARGV[@]}" "$IMG" sleep 45)
    printf 'argv: runuser -u admin -- env -i … systemd-run --user --scope --unit=%s -p Delegate=yes --' "$U"; printf ' %q' "${ARGV[@]}"; echo
    setsid runuser -u $ADMIN -- "${ENVA[@]}" systemd-run --user --quiet --scope --unit="$U" -p Delegate=yes -- \
        "${ARGV[@]}" > "$OUT/run.log" 2>&1 < /dev/null &
    TARGET=/user.slice/user-$ADMIN_UID.slice/user@$ADMIN_UID.service/app.slice/$U.scope ;;
  *) echo "unknown shape $shape"; exit 2 ;;
  esac
}

launch
sleep 8
echo "--- launch output"; cat "$OUT/run.log"
G2=("${G[0]}" "${EXTRA_GLOBAL[@]}" "${G[@]:1}")
CPID=$(as_admin "${G2[@]}" inspect --format '{{.State.ConmonPid}}' "$NAME" 2>/dev/null)
as_admin "${G2[@]}" inspect --format 'inspect: State={{.State.Status}} Cgroups={{.HostConfig.Cgroups}} CgroupParent={{.HostConfig.CgroupParent}} CgroupManager={{.HostConfig.CgroupManager}} ConmonPid={{.State.ConmonPid}} Pid={{.State.Pid}}' "$NAME" 2>&1
CFG=$(as_admin "${G2[@]}" inspect --format '{{.OCIConfigPath}}' "$NAME" 2>/dev/null)
[ -n "$CFG" ] && echo "OCI linux.cgroupsPath = $(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['linux'].get('cgroupsPath'))" "$CFG")"
# the target scope for split is wherever systemd put it; resolve it from the
# podman CLI's own cgroup if the guess is absent
if [ ! -d "/sys/fs/cgroup$TARGET" ] && [ "$shape" = split ]; then
  for p in $(pgrep -u $ADMIN -x podman); do c=$(sed 's/^0:://' /proc/$p/cgroup); case $c in *"$U.scope"*) TARGET=${c%%"$U.scope"*}$U.scope;; esac; done
fi
echo "--- target cgroup: $TARGET"
echo "controllers: $(cat /sys/fs/cgroup$TARGET/cgroup.controllers 2>/dev/null) | memory.max=$(cat /sys/fs/cgroup$TARGET/memory.max 2>/dev/null) pids.max=$(cat /sys/fs/cgroup$TARGET/pids.max 2>/dev/null) owner=$(stat -c %U /sys/fs/cgroup$TARGET 2>/dev/null)"
echo "--- recursive cgroup.procs of the target"
cgroup_tree_procs "$TARGET"
echo "--- every sandbox process (conmon subtree) classified against the target"
if [ -n "$CPID" ] && [ -d /proc/$CPID ]; then
  out=0; n=0
  while read -r line; do
    case $line in pid=*)
      n=$((n+1)); cg=${line##*cgroup=}
      case $cg in "$TARGET"|"$TARGET"/*) v=INSIDE ;; *) v=OUTSIDE; out=$((out+1)) ;; esac
      echo "$v ${line%% cgroup=*} cgroup=$cg" ;;
    esac
  done < <(proc_report "$CPID")
  echo "VERDICT shape=$shape: $((n-out))/$n sandbox processes inside $TARGET$( [ $out -eq 0 ] && [ $n -gt 0 ] && echo ' -> CONTAINS ALL' || echo ' -> DOES NOT CONTAIN ALL')"
else
  echo "VERDICT shape=$shape: no running sandbox (conmon pid '${CPID}') -> launch FAILED"
fi
echo "--- podman CLI / runuser / systemd-run processes of this launch"
for p in $(pgrep -f "$NAME" 2>/dev/null); do [ "$p" = $$ ] && continue
  echo "pid=$p comm=$(cat /proc/$p/comm 2>/dev/null) cgroup=$(sed 's/^0:://' /proc/$p/cgroup 2>/dev/null)"; done
echo "--- stop"
as_admin "${G2[@]}" stop -t 2 "$NAME" >/dev/null 2>&1; as_admin "${G2[@]}" rm -f "$NAME" >/dev/null 2>&1
sleep 3
systemctl stop "$U.service" "$U.scope" 2>/dev/null
for p in /proc/[0-9]*; do e=$(readlink $p/exe 2>/dev/null) || continue
  case $e in /usr/libexec/qdistro/runsc/*|/usr/bin/conmon) echo "LEFTOVER pid=${p#/proc/} exe=$e";; esac; done; echo "(end leftovers)"
echo "target after stop: $( [ -d /sys/fs/cgroup$TARGET ] && { echo exists; cgroup_tree_procs "$TARGET"; } || echo gone)"
