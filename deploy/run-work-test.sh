#!/bin/bash
# Run inside VM as root. Launches the test as work in background.
set -u
sudo -u work bash -c '
  exec > /tmp/test-output.txt 2>&1
  echo "[work] starting at $(date -Iseconds)"
  echo "[work] python: $(which python3)"
  echo "[work] importing qdistro_app..."
  python3 -c "import qdistro_app; print(qdistro_app)" || { echo "[work] import FAILED"; exit 9; }
  echo "[work] calling request..."
  python3 /usr/local/bin/qdistro-test-permission
  rc=$?
  echo "[work] script returned rc=$rc"
' &
echo "$!" > /tmp/work-test-pid
echo "[run-work-test] launched, parent pid=$!"
