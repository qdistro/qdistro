#!/bin/sh
# qdistro-tier3s-smoke — default command of qdistro/tier3s-headless-smoke.
# Prints one `SMOKE <key>=<value>` line per fact the A-iii driver asserts
# (tier3s/CONTRACT.md §7); the driver, not this script, decides PASS/FAIL.
# Exits 0 unless the shell itself breaks. `--hold [SECONDS]` (default 600)
# then stays up until SIGTERM (exit 0) or the time runs out, so a driver can
# stop a LIVE launch (podman stop, session-manager stop, SIGKILL).
smoke_uid=$(id -u)
echo "SMOKE snapshot=$(cat /etc/qdistro/tier3s-image 2>/dev/null | sed -n 's/^SNAPSHOT=//p')"
echo "SMOKE kernel=$(cat /proc/version)"
echo "SMOKE dmesg=$(dmesg --syslog 2>&1 | head -1)"
echo "SMOKE id=$(id)"
# passwd_self: keep-id synthesizes an entry for the caller's own uid (the
# silo's host uid — model A); passwd_admin: the image's baked uid-1000 entry
echo "SMOKE passwd_self=$(getent passwd "$smoke_uid")"
echo "SMOKE passwd_admin=$(getent passwd 1000)"
echo "SMOKE home=$HOME"
echo "SMOKE lang=$LANG charmap=$(locale charmap 2>&1) utf8_locales=$(locale -a 2>/dev/null | grep -ic 'utf-\?8')"
for d in "/run/user/$smoke_uid" /home/admin /tmp; do
    echo "SMOKE mount $d=$(stat -c '%u:%g %a' "$d" 2>&1)"
done
# posture as the sandboxed process sees it (gVisor's /proc and mount table)
st() { sed -n "s/^$1:[[:space:]]*//p" /proc/self/status; }
echo "SMOKE caps=inh:$(st CapInh),prm:$(st CapPrm),eff:$(st CapEff),bnd:$(st CapBnd),amb:$(st CapAmb)"
echo "SMOKE nnp=$(st NoNewPrivs) seccomp=$(st Seccomp)"
# (no awk in the image: read the mount table with the shell)
mnt() { while read -r _ mp ty opts _; do [ "$mp" = "$1" ] && { echo "$ty:$opts"; break; }; done < /proc/self/mounts; }
r=$(mnt /); r=${r#*:}; echo "SMOKE rootfs=${r%%,*}"
for d in "/run/user/$smoke_uid" /home/admin; do echo "SMOKE mountopts $d=$(mnt "$d")"; done
# /etc sits on the read-only rootfs (unlike $HOME, which is a writable
# tmpfs). Under keep-id the guest uid owns NOTHING on the rootfs, so the
# denial surfaces as EACCES — EROFS is only reachable by a uid that owns a
# rootfs path; either errno proves the write failed.
touch /etc/.smoke-rw 2>/tmp/smoke-rw.err; echo "SMOKE rootfs_write rc=$? err=$(cat /tmp/smoke-rw.err)"
f=/tmp/smoke-chmod
: > "$f"
chmod 600 "$f" 2>/dev/null; echo "SMOKE chmod rc=$? mode=$(stat -c %a "$f")"
chmod -h 640 "$f" 2>/dev/null; echo "SMOKE chmod_nofollow rc=$? mode=$(stat -c %a "$f")"
ls -l /tmp > /dev/null 2>/tmp/smoke-ls.err; echo "SMOKE ls_l rc=$? stderr_bytes=$(wc -c < /tmp/smoke-ls.err)"
echo "SMOKE routes=$(ip -o route show 2>&1 | wc -l) links=$(ip -o link show 2>/dev/null | cut -d: -f2 | tr -d ' ' | tr '\n' ',')"
echo "SMOKE route_table=$(ip -o route show table all 2>&1 | tr -s ' ' | tr '\n' ';')"
echo "SMOKE done"
if [ "${1:-}" = --hold ]; then
    trap 'echo "SMOKE term"; exit 0' TERM INT
    echo "SMOKE holding ${2:-600}s"
    sleep "${2:-600}" &
    wait $!
fi
