#!/bin/bash
pkill -9 -x weston 2>/dev/null
pkill -9 -f sdl-freerdp 2>/dev/null
pkill -9 -f qdshell.py 2>/dev/null
pkill -9 -f qdistro-tier3 2>/dev/null
pkill -9 -f waypipe 2>/dev/null
pkill -9 -f qdistro-test-window 2>/dev/null
pkill -9 -f qdistro-test-clipboard-source 2>/dev/null
pkill -9 -f wl-copy 2>/dev/null
pkill -9 -f wl-paste 2>/dev/null
pkill -9 -f admin-approval-app 2>/dev/null
rm -f /tmp/qdistro-tier3-*.sock /tmp/qdshell-*.sock 2>/dev/null
sleep 2
echo "cleanup done"
pgrep -ax weston || echo "no weston processes"
