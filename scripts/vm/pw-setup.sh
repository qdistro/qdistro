#!/bin/bash
# S0 spike helper: start pipewire daemon as admin + probe its state.
set -eo pipefail

pkill -9 pipewire 2>/dev/null || true
sleep 1

install -d -o admin -g admin -m 0700 /run/user/1000 2>/dev/null || true

runuser -u admin -- bash -c '
  export XDG_RUNTIME_DIR=/run/user/1000
  export HOME=/home/admin
  nohup pipewire >/home/admin/pw.log 2>&1 </dev/null &
  echo pw-pid=$!
  sleep 2
  nohup wireplumber >/home/admin/wp.log 2>&1 </dev/null &
  echo wp-pid=$!
  sleep 1
  pgrep -af pipewire | head -3
  echo ---list-nodes---
  pw-cli list-objects Node 2>/dev/null | head -20 || echo "pw-cli failed"
'
