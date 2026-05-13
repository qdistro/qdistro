#!/bin/bash
pkill -9 -x weston 2>/dev/null
pkill -9 -f sdl-freerdp 2>/dev/null
pkill -9 -f qdshell 2>/dev/null
pkill -9 -f waypipe 2>/dev/null
pkill -9 -f qdistro-tier3 2>/dev/null
pkill -9 -f qdistro-test 2>/dev/null
pkill -9 -f admin-approval 2>/dev/null
pkill -9 -f qdistro-nested-pixelfeed 2>/dev/null
rm -f /tmp/qd*.sock /tmp/qdshell-*.sock /tmp/qdistro-tier3-*.sock 2>/dev/null
sleep 2
echo "=== leftovers ==="
pgrep -af weston | head -5
pgrep -af waypipe | head -5
pgrep -af qdshell | head -5
echo "=== ports ==="
ss -tnlp 2>/dev/null | grep 3389 || echo "3389 clean"
echo "=== pipewire ==="
ls /run/user/1000/pipewire* 2>/dev/null || echo "pipewire socket missing"
