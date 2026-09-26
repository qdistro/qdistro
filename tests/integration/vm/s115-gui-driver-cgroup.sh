#!/usr/bin/env bash
set -euo pipefail
lib=/tmp/qci-gui-waiters-cgroup.sh
root=/tmp/qci-driver-cgroup-accept-$$
mkdir -p "$root"

# A direct owner SIGKILL immediately after its foreground child starts.
d=$root/direct
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  bash -c '\''echo child-ready > "$1/ready"; for i in $(seq 1 100); do echo tick >> "$1/log"; sleep 0.03; done'\'' _ "$2"
  echo DRIVER-AFTER >> "$2/log"
' _ "$lib" "$d" >/dev/null 2>&1 &
owner=$!
for i in $(seq 1 100); do [ -s "$d/ready" ] && break; sleep 0.05; done
[ -s "$d/ready" ] || { echo 'direct child never started'; exit 1; }
kill -KILL "$owner"
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo CLAIMED >> "$2/log"' _ "$lib" "$d"
wait "$owner" 2>/dev/null || true
sleep 0.3
[ "$(tail -n 1 "$d/log")" = CLAIMED ] || { tail "$d/log"; exit 1; }
! grep -q DRIVER-AFTER "$d/log"
echo 'PASS direct owner SIGKILL drains foreground child'

# Short-lived forkers leave orphaned children that remain capable of writing.
d=$root/forks
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  bash -c '\''for i in $(seq 1 80); do ( (echo ready >> "$1/ready"; sleep 0.6; echo FORK-TICK >> "$1/log") & ); sleep 0.005; done; wait'\'' _ "$2"
  echo DRIVER-AFTER >> "$2/log"
' _ "$lib" "$d" >/dev/null 2>&1 &
owner=$!
for i in $(seq 1 100); do [ -s "$d/ready" ] && break; sleep 0.05; done
[ -s "$d/ready" ] || { echo 'forkers never started'; exit 1; }
kill -KILL "$owner"
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo CLAIMED >> "$2/log"' _ "$lib" "$d"
wait "$owner" 2>/dev/null || true
sleep 0.8
[ "$(tail -n 1 "$d/log")" = CLAIMED ] || { tail "$d/log"; exit 1; }
! grep -q DRIVER-AFTER "$d/log"
echo 'PASS forked orphans cannot act after contender'

# An intentional completion can leave a detached GUI-like app for inspection.
d=$root/done
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  setsid -f bash -c '\''sleep 0.5; echo APP-SURVIVED > "$1/app"'\'' _ "$2" </dev/null >/dev/null 2>&1
  qci_claim_done
' _ "$lib" "$d"
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo CLAIMED > "$2/claimed"' _ "$lib" "$d"
for i in $(seq 1 100); do [ -s "$d/app" ] && break; sleep 0.05; done
[ -s "$d/app" ] && [ -s "$d/claimed" ]
echo 'PASS intentional completion preserves detached app without holding lock'

# A bg_start worker moves out of the driver scope before it runs.
d=$root/job
mkdir -p "$d"
bash -c '
  source "$1"; QCI_BG_DIR=$2; qci_claim_driver "$2/driver.lock"
  bg_start job - "sleep 1; echo JOB-DONE > $2/job-done"
' _ "$lib" "$d"
if QCI_DRIVER_CLAIM_GRACE=0.1 bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo EARLY > "$2/early"' _ "$lib" "$d" >/dev/null 2>&1; then
  echo 'contender acquired while job was active'; exit 1
fi
for i in $(seq 1 200); do [ -s "$d/job-done" ] && break; sleep 0.05; done
[ -s "$d/job-done" ] && [ ! -e "$d/early" ]
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo LATE > "$2/late"' _ "$lib" "$d"
[ -s "$d/late" ]
echo 'PASS registered worker holds claim after owner exit'
