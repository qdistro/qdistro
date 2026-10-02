#!/bin/sh
# qdistro-tier3s-smoke — default command of qdistro/tier3s-headless-smoke.
# Prints one `SMOKE <key>=<value>` line per fact the A-iii driver asserts
# (tier3s/CONTRACT.md §7); the driver, not this script, decides PASS/FAIL.
# Exits 0 unless the shell itself breaks.
echo "SMOKE snapshot=$(cat /etc/qdistro/tier3s-image 2>/dev/null | sed -n 's/^SNAPSHOT=//p')"
echo "SMOKE kernel=$(cat /proc/version)"
echo "SMOKE dmesg=$(dmesg --syslog 2>&1 | head -1)"
echo "SMOKE id=$(id)"
echo "SMOKE passwd=$(getent passwd 1000)"
echo "SMOKE home=$HOME"
echo "SMOKE lang=$LANG charmap=$(locale charmap 2>&1) utf8_locales=$(locale -a 2>/dev/null | grep -ic 'utf-\?8')"
for d in /run/user/1000 /home/admin/.cache /tmp; do
    echo "SMOKE mount $d=$(stat -c '%u:%g %a' "$d" 2>&1)"
done
f=/tmp/smoke-chmod
: > "$f"
chmod 600 "$f" 2>/dev/null; echo "SMOKE chmod rc=$? mode=$(stat -c %a "$f")"
chmod -h 640 "$f" 2>/dev/null; echo "SMOKE chmod_nofollow rc=$? mode=$(stat -c %a "$f")"
ls -l /tmp > /dev/null 2>/tmp/smoke-ls.err; echo "SMOKE ls_l rc=$? stderr_bytes=$(wc -c < /tmp/smoke-ls.err)"
echo "SMOKE routes=$(ip -o route show 2>&1 | wc -l) links=$(ip -o link show 2>/dev/null | cut -d: -f2 | tr -d ' ' | tr '\n' ',')"
echo "SMOKE done"
