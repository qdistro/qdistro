#!/usr/bin/env bash
set -euo pipefail
lib=/tmp/qci-gui-waiters-cgroup.sh
root=/tmp/qci-driver-cgroup-accept-$$
mkdir -p "$root"
await_cleanup() {
  local d=$1 scope marker i
  scope=$(cat "$d/scope")
  marker=$(cat "$d/marker")
  for i in $(seq 1 100); do
    [ ! -e "$scope" ] && [ ! -e "$marker" ] && return 0
    sleep 0.05
  done
  echo "stale claim scope or marker: $scope $marker"
  return 1
}

# A direct owner SIGKILL immediately after its foreground child starts.
d=$root/direct
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
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
await_cleanup "$d"
echo 'PASS direct owner SIGKILL drains foreground child'

# Short-lived forkers leave orphaned children that remain capable of writing.
d=$root/forks
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
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
await_cleanup "$d"
echo 'PASS forked orphans cannot act after contender'

# An intentional completion can leave a detached GUI-like app for inspection.
d=$root/done
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
  setsid -f bash -c '\''sleep 0.5; echo APP-SURVIVED > "$1/app"'\'' _ "$2" </dev/null >/dev/null 2>&1
  qci_claim_done
' _ "$lib" "$d"
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo CLAIMED > "$2/claimed"' _ "$lib" "$d"
for i in $(seq 1 100); do [ -s "$d/app" ] && break; sleep 0.05; done
[ -s "$d/app" ] && [ -s "$d/claimed" ]
await_cleanup "$d"
echo 'PASS intentional completion preserves detached app without holding lock'

# A bg_start worker moves out of the driver scope before it runs.
d=$root/job
mkdir -p "$d"
bash -c '
  source "$1"; QCI_BG_DIR=$2; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
  bg_start job - "sleep 1; echo JOB-DONE > $2/job-done"
' _ "$lib" "$d"
if QCI_DRIVER_CLAIM_GRACE=0.1 bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo EARLY > "$2/early"' _ "$lib" "$d" >/dev/null 2>&1; then
  echo 'contender acquired while job was active'; exit 1
fi
for i in $(seq 1 200); do [ -s "$d/job-done" ] && break; sleep 0.05; done
[ -s "$d/job-done" ] && [ ! -e "$d/early" ]
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo LATE > "$2/late"' _ "$lib" "$d"
[ -s "$d/late" ]
await_cleanup "$d"
echo 'PASS registered worker holds claim after owner exit'

# Freeze the owner at the precise handoff after migration but before CONT.
# The guardian must kill the stopped pending worker, or the next claim hangs.
d=$root/pending
mkdir -p "$d"
bash -c '
  source "$1"; QCI_BG_DIR=$2; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
  pending_dir=$2
  kill() {
    if [ "$1" = -CONT ]; then
      echo "$2" > "$pending_dir/moved"
      while :; do sleep 1; done
    fi
    builtin kill "$@"
  }
  bg_start pending - "echo JOB-RAN > $2/job-ran"
' _ "$lib" "$d" >/dev/null 2>&1 &
owner=$!
for i in $(seq 1 100); do [ -s "$d/moved" ] && break; sleep 0.05; done
[ -s "$d/moved" ] || { echo 'pending job was never migrated'; exit 1; }
kill -KILL "$owner"
bash -c 'source "$1"; qci_claim_driver "$2/driver.lock"; echo CLAIMED > "$2/claimed"' _ "$lib" "$d"
wait "$owner" 2>/dev/null || true
[ -s "$d/claimed" ] && [ ! -e "$d/job-ran" ]
await_cleanup "$d"
echo 'PASS owner death during job handoff kills stopped worker'

# A plain successful driver with no app leaves no cgroup or marker.
d=$root/ordinary
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
' _ "$lib" "$d"
await_cleanup "$d"
echo 'PASS ordinary completion removes empty scope and marker'

# A refused contender must remove only its own empty scope and marker.
d=$root/refused
mkdir -p "$d"
bash -c '
  source "$1"; qci_claim_driver "$2/driver.lock"
  echo "$QCI_DRIVER_CLAIM_SCOPE" > "$2/scope"
  echo "$QCI_DRIVER_CLAIM_DONE_MARKER" > "$2/marker"
  echo ready > "$2/ready"
  sleep 10
' _ "$lib" "$d" >/dev/null 2>&1 &
owner=$!
for i in $(seq 1 100); do [ -s "$d/ready" ] && break; sleep 0.05; done
[ -s "$d/ready" ] || { echo 'holder for refusal never started'; exit 1; }
if QCI_DRIVER_CLAIM_GRACE=0.1 bash -c '
  source "$1"
  me=$BASHPID; start=$(_qci_proc_start "$me")
  rel=$(awk -F: '\''$1 == 0 && $2 == "" { print $3 }'\'' /proc/self/cgroup)
  echo "/sys/fs/cgroup$rel/qci-driver-$me-$start" > "$2/refused-scope"
  echo "$2/driver.lock.done.$me.$start" > "$2/refused-marker"
  qci_claim_driver "$2/driver.lock"
  echo BAD > "$2/bad"
' _ "$lib" "$d" >/dev/null 2>&1; then
  echo 'contender unexpectedly claimed'; exit 1
fi
[ ! -e "$(cat "$d/refused-scope")" ] && [ ! -e "$(cat "$d/refused-marker")" ]
[ ! -e "$d/bad" ]
kill -KILL "$owner"
wait "$owner" 2>/dev/null || true
await_cleanup "$d"
echo 'PASS refused claim cleans its scope and marker'

# An unsafe pre-existing marker prevents the claim; the new empty scope is
# removed, while the planted symlink is left untouched for diagnosis.
d=$root/setup-failure
mkdir -p "$d"
if bash -c '
  source "$1"
  me=$BASHPID; start=$(_qci_proc_start "$me")
  rel=$(awk -F: '\''$1 == 0 && $2 == "" { print $3 }'\'' /proc/self/cgroup)
  scope=/sys/fs/cgroup$rel/qci-driver-$me-$start
  marker=$2/driver.lock.done.$me.$start
  echo "$scope" > "$2/scope"
  echo "$marker" > "$2/marker"
  ln -s /etc/passwd "$marker"
  qci_claim_driver "$2/driver.lock"
  echo BAD > "$2/bad"
' _ "$lib" "$d" >/dev/null 2>&1; then
  echo 'unsafe marker unexpectedly accepted'; exit 1
fi
[ ! -e "$(cat "$d/scope")" ] && [ -L "$(cat "$d/marker")" ] && [ ! -e "$d/bad" ]
rm -f -- "$(cat "$d/marker")"
echo 'PASS failed setup removes empty scope without following marker'
