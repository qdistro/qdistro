#!/bin/bash
# labwc-present-frame.sh — GUEST side of vm-gui's labwc frame refresh.
#
# vm-gui pipes this file to the guest (vm-script) before every host capture.
# It makes labwc render and commit a frame from its clients' CURRENT buffers
# by requesting one wlr-screencopy (grim), and returns that frame so the host
# can wait until `virsh screenshot` shows it (see labwc_present_frame in
# vm-gui for why). Output, one line each, parsed by prefix on the host:
#   qci-labwc-frame=<base64 PNG, cursor included>   (only with refresh=ok)
#   qci-labwc-refresh=ok|not-labwc|ambiguous|probe-failed|no-socket|no-grim|grim-failed
#
# The screencopy must reach labwc itself, not any compositor that answers:
# the labwc lane can also have qdwin's socket (wayland-1) in the same runtime
# dir. labwc exports its socket name to the processes it starts (session,
# autostart, swaybg), so the name is read from its children's environment,
# and more than one labwc, or children that disagree, is refused.
#
# QCI_PROC_ROOT and QCI_RUNTIME_ROOT exist for the host-side tests only.
set -u -o pipefail
proc=${QCI_PROC_ROOT:-/proc}

# pgrep: 0 = found, 1 = no such process; anything else (missing pgrep, an
# unreadable /proc) is a failed probe, which must not read as "no labwc".
rc=0; pids=$(pgrep -x labwc 2>/dev/null) || rc=$?
if [ "$rc" -eq 1 ] && [ -z "$pids" ]; then echo qci-labwc-refresh=not-labwc; exit 0; fi
if [ "$rc" -ne 0 ] || [ -z "$pids" ]; then echo qci-labwc-refresh=probe-failed; exit 0; fi
if [ "$(printf '%s\n' "$pids" | grep -c .)" -ne 1 ]; then echo qci-labwc-refresh=ambiguous; exit 0; fi
pid=$pids

names=""
rc=0; children=$(pgrep -P "$pid" 2>/dev/null) || rc=$?
if [ "$rc" -gt 1 ]; then echo qci-labwc-refresh=probe-failed; exit 0; fi
for child in $children; do
    wd=$(tr '\0' '\n' < "$proc/$child/environ" 2>/dev/null | sed -n 's/^WAYLAND_DISPLAY=//p' | head -n1)
    [ -n "$wd" ] && names="$names$wd"$'\n'
done
names=$(printf '%s' "$names" | sort -u | grep -v '^$')
if [ "$(printf '%s\n' "$names" | grep -c .)" -ne 1 ]; then echo qci-labwc-refresh=no-socket; exit 0; fi
case "$names" in */*|*..*) echo qci-labwc-refresh=no-socket; exit 0 ;; esac

uid=$(awk '/^Uid:/ {print $2; exit}' "$proc/$pid/status" 2>/dev/null)
user=$(getent passwd "$uid" 2>/dev/null | cut -d: -f1)
runtime="${QCI_RUNTIME_ROOT:-/run/user}/$uid"
if [ -z "$user" ] || [ ! -S "$runtime/$names" ]; then echo qci-labwc-refresh=no-socket; exit 0; fi

command -v grim >/dev/null 2>&1 || { echo qci-labwc-refresh=no-grim; exit 0; }
frame=$(runuser -u "$user" -- env XDG_RUNTIME_DIR="$runtime" WAYLAND_DISPLAY="$names" \
            grim -c -t png - 2>/dev/null | base64 -w0) || frame=""
if [ -z "$frame" ]; then echo qci-labwc-refresh=grim-failed; exit 0; fi
echo "qci-labwc-frame=$frame"
echo qci-labwc-refresh=ok
