#!/bin/bash
# tier3s/spike/s2-waypipe.sh — Phase S step 2 (waypipe SHM round trip), as
# root INSIDE the dev VM. THROWAWAY. Host side orchestrates and screenshots.
#
#   s2-waypipe.sh info [--no-host-uds]   client + sandboxed `waypipe server -- wayland-info`
#   s2-waypipe.sh start <weston-terminal|foot>
#   s2-waypipe.sh status <app>
#   s2-waypipe.sh stop <app>
#
# Topology (03 Phase S step 2, dev lane, no secctx):
#   admin: waypipe -s $D/link.sock -o --no-gpu client     -> qdwin wayland-1
#   admin: podman <tier3s flags> --runtime-flag=host-uds=open
#            run … -v $D:/run/qdistro/link:rw IMG
#            waypipe -s /run/qdistro/link/link.sock -o --no-gpu server -- <app>
# Long-running halves run in root transient units (systemd-run) that drop to
# admin with runuser, so nothing stays attached to the qemu-ga exec.
set -uo pipefail
. "$(dirname "$0")/lib.sh"
WL=wayland-1                      # qdwin's socket in the dev VM
sub=${1:?usage}; shift

bridge_dir() { echo "$ADMIN_RT/t3s-bridge-$1"; }
app_argv() {
    case $1 in
      weston-terminal) echo "weston-terminal --shell=/usr/local/bin/t3s-banner" ;;
      foot)            echo "foot /usr/local/bin/t3s-banner" ;;
      wayland-info)    echo "wayland-info" ;;
      *) echo "unknown app $1" >&2; return 1 ;;
    esac
}

start_client() {  # $1 = tag
    local D; D=$(bridge_dir "$1")
    rm -rf "$D"; install -d -o $ADMIN -g $ADMIN -m 0700 "$D"
    systemctl stop "t3s-s2-client-$1" 2>/dev/null; systemctl reset-failed "t3s-s2-client-$1" 2>/dev/null
    local cmd=(waypipe -s "$D/link.sock" -o --no-gpu --title-prefix "[tier3s] " client)
    printf 'client argv (as admin, WAYLAND_DISPLAY=%s):' "$WL"; printf ' %q' "${cmd[@]}"; echo
    systemd-run --quiet --unit="t3s-s2-client-$1" --collect -p StandardOutput=file:"$WORK/s2-$1/client.log" \
        -p StandardError=file:"$WORK/s2-$1/client.log" -- \
        runuser -u $ADMIN -- env -i PATH=/usr/bin:/bin HOME=/home/$ADMIN XDG_RUNTIME_DIR=$ADMIN_RT \
        WAYLAND_DISPLAY=$WL bash -c 'umask 0177; exec "$@"' bash "${cmd[@]}"
    for _ in $(seq 1 50); do [ -S "$D/link.sock" ] && break; sleep 0.1; done
    ls -l "$D"
}

sandbox_argv() {  # $1 = tag, $2 = app, $3 = host-uds (open|none)
    local D; D=$(bridge_dir "$1")
    T3S_RTFLAGS=(--runtime-flag=debug "--runtime-flag=debug-log=$WORK/s2-$1/runsc-debug/")
    [ "$3" = open ] && T3S_RTFLAGS+=(--runtime-flag=host-uds=open)
    T3S_RUNOPTS=(--name "t3s-s2-$1" -v "$D:/run/qdistro/link:rw"
        -e XDG_RUNTIME_DIR=/run/user/1000 -e HOME=/tmp -e LANG=C.UTF-8)
    t3s_podman_argv
    # shellcheck disable=SC2207
    SBX_ARGV=("${T3S_ARGV[@]}" "$IMG" waypipe -s /run/qdistro/link/link.sock -o --no-gpu server -- $(app_argv "$2"))
}

prep() {  # $1 = tag
    rm -rf "$WORK/s2-$1"; mkdir -p "$WORK/s2-$1/runsc-debug"; chown -R $ADMIN: "$WORK/s2-$1"
    t3s_global; as_admin "${T3S_GLOBAL[@]}" rm -f "t3s-s2-$1" >/dev/null 2>&1
}

denials() {  # $1 = tag : seccomp denials + unsupported syscalls from the Sentry log
    echo "--- Sentry: seccomp denials / unsupported syscalls (x86_64 nr)"
    grep -h -E 'denied by seccomp|Unsupported syscall' "$WORK/s2-$1"/runsc-debug/*boot* 2>/dev/null \
        | sed -E 's/^.*\] //' | sed -E 's/\(0x[^)]*\)/(…)/' | cut -c1-110 | sort | uniq -c
    echo "(end denials)"
}

case $sub in
info)
    hu=open; tag=info; [ "${1:-}" = --no-host-uds ] && { hu=none; tag=info-nohostuds; }
    prep $tag; start_client $tag; sandbox_argv $tag wayland-info $hu
    printf 'sandbox argv (as admin):'; printf ' %q' "${SBX_ARGV[@]}"; echo
    t0=$(date +%s.%N)
    as_admin timeout 60 "${SBX_ARGV[@]}" > "$WORK/s2-$tag/wayland-info.txt" 2>&1; rc=$?
    t1=$(date +%s.%N)
    echo "sandbox rc=$rc wall=$(python3 -c "print(round($t1-$t0,2))")s"
    echo "--- wayland-info output: $(wc -l < "$WORK/s2-$tag/wayland-info.txt") lines; interfaces:"
    grep -oE "interface: '[a-z_0-9]+'" "$WORK/s2-$tag/wayland-info.txt" | sort -u | tr '\n' ' '; echo
    grep -E 'name:|width:|refresh' "$WORK/s2-$tag/wayland-info.txt" | head -6
    tail -5 "$WORK/s2-$tag/wayland-info.txt"
    sleep 1
    echo "--- client unit after the one-shot connection: $(systemctl is-active t3s-s2-client-$tag)"
    echo "--- client log"; cat "$WORK/s2-$tag/client.log"
    denials $tag
    systemctl stop "t3s-s2-client-$tag" 2>/dev/null
    ;;
start)
    app=${1:?app}; prep "$app"; start_client "$app"; sandbox_argv "$app" "$app" open
    printf 'sandbox argv (as admin, in unit t3s-s2-sbx-%s):' "$app"; printf ' %q' "${SBX_ARGV[@]}"; echo
    systemctl stop "t3s-s2-sbx-$app" 2>/dev/null; systemctl reset-failed "t3s-s2-sbx-$app" 2>/dev/null
    systemd-run --quiet --unit="t3s-s2-sbx-$app" --collect -p StandardOutput=file:"$WORK/s2-$app/sandbox.log" \
        -p StandardError=file:"$WORK/s2-$app/sandbox.log" -- \
        runuser -u $ADMIN -- env -i PATH=/usr/bin:/bin HOME=/home/$ADMIN USER=$ADMIN LOGNAME=$ADMIN \
        XDG_RUNTIME_DIR=$ADMIN_RT DBUS_SESSION_BUS_ADDRESS=unix:path=$ADMIN_RT/bus "${SBX_ARGV[@]}"
    sleep 8
    echo "units: client=$(systemctl is-active t3s-s2-client-$app) sandbox=$(systemctl is-active t3s-s2-sbx-$app)"
    ;;
status)
    app=${1:?app}
    echo "units: client=$(systemctl is-active t3s-s2-client-$app) sandbox=$(systemctl is-active t3s-s2-sbx-$app)"
    t3s_global
    as_admin "${T3S_GLOBAL[@]}" inspect --format 'OCIRuntime={{.OCIRuntime}} State={{.State.Status}} Pid={{.State.Pid}} ConmonPid={{.State.ConmonPid}} Mounts={{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}' "t3s-s2-$app" 2>&1
    CPID=$(as_admin "${T3S_GLOBAL[@]}" inspect --format '{{.State.ConmonPid}}' "t3s-s2-$app" 2>/dev/null)
    echo "--- sandbox process tree (conmon $CPID)"; [ -n "$CPID" ] && proc_report "$CPID" | grep -v '^    cmdline=$'
    echo "--- host waypipe client (unit t3s-s2-client-$app)"
    for p in $(cat /sys/fs/cgroup/system.slice/t3s-s2-client-$app.service/cgroup.procs 2>/dev/null); do
        echo "pid=$p comm=$(cat /proc/$p/comm) uid=$(awk '/^Uid:/{print $2}' /proc/$p/status) exe=$(readlink /proc/$p/exe)"; done
    echo "--- host processes named like the app (expect none: the app runs inside the Sentry)"
    ps -eo pid,user,comm | awk -v a="$app" '$3 == a || $3 == "t3s-banner" || ($3 == "waypipe" && $2 != "admin")' ; echo "(end)"
    ps -eo pid,user,comm,args | grep -E '[w]aypipe' | cut -c1-160
    echo "--- sandbox log"; cat "$WORK/s2-$app/sandbox.log"
    echo "--- client log"; cat "$WORK/s2-$app/client.log"
    denials "$app"
    ;;
stop)
    app=${1:?app}; t3s_global
    as_admin "${T3S_GLOBAL[@]}" stop -t 3 "t3s-s2-$app" 2>&1; echo "podman stop rc=$?"
    sleep 2
    systemctl stop "t3s-s2-sbx-$app" "t3s-s2-client-$app" 2>/dev/null
    echo "units: client=$(systemctl is-active t3s-s2-client-$app) sandbox=$(systemctl is-active t3s-s2-sbx-$app)"
    as_admin podman ps -a --format '{{.Names}} {{.Status}}'; echo "(end ps -a)"
    for p in /proc/[0-9]*; do e=$(readlink $p/exe 2>/dev/null) || continue
      case $e in /usr/libexec/qdistro/runsc/*|/usr/bin/waypipe) echo "LEFTOVER pid=${p#/proc/} exe=$e";; esac; done; echo "(end leftovers)"
    denials "$app"
    ;;
*) echo "unknown subcommand $sub" >&2; exit 2 ;;
esac
