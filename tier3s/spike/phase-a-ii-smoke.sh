#!/bin/bash
# tier3s/spike/phase-a-ii-smoke.sh — GUEST side (root, dev test VM). Milestone
# A-ii smoke: the installer put THIS branch's tier3s artifacts in place before
# any tier3s unit ran, and one headless-smoke launch through the new
# SessionManager1.CreateTier3sSilo + StartSilo comes up and stops cleanly. A
# smoke, not the A-iii DONE-bar drivers (s120-s122). Asserting: each check
# prints `CHECK <name>: OK|BAD ...`; the step's exit status is its BAD count.
#   phase-a-ii-smoke.sh <step>
#     installed <expect-file>   expect-file lines: <sha256> <mode> <installed path>
#     reinstall <expect-file>   re-run install-session-manager.sh from the staged
#                               checkout, then the same checks
#     provision | image | rule | smoke-exit | smoke-live | final
set -u
SRC=/root/qdistro-src
cd "$SRC" || exit 99
BAD=0
ok()  { echo "CHECK $1: OK${2:+ ($2)}"; }
bad() { echo "CHECK $1: BAD${2:+ ($2)}"; BAD=$((BAD + 1)); }
is()  { if [ "$2" = "$3" ]; then ok "$1" "$2"; else bad "$1" "got '$2', want '$3'"; fi; }
finish() { echo "### BAD=$BAD"; exit "$BAD"; }
UNIT=qdistro-tier3s-silo@smoke.service
CTR=qdistro-tier3s-smoke
PIN=tier3s/RUNSC_RELEASE
ACTION="qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke"
as_admin() {
    runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin LOGNAME=admin \
        XDG_RUNTIME_DIR=/run/user/1000 "$@"
}
sm() { as_admin busctl --system call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
        org.qdistro.SessionManager1 "$@"; }
silo_state() {   # the smoke silo's state from ListSilos (JSON)
    sm ListSilos | sed 's/^s "//; s/"$//; s/\\"/"/g' | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    if s["name"] == "smoke": print(s["state"]); break
else: print("absent")'
}
unit_log() {   # this unit's journal since a cursor file (scoped to the unit, never the whole journal)
    journalctl -u "$UNIT" --no-pager -o cat --after-cursor="$(cat /var/tmp/t3s-cursor)" 2>/dev/null
}
scope_log() {   # <token>: the workload's output. journald files it under the owning scope,
    # where podman and conmon (the processes that write it) run, not under the launch unit
    journalctl _SYSTEMD_UNIT="qdistro-tier3s-$1.scope" --no-pager -o cat 2>/dev/null
}
mark_cursor() { journalctl -n 0 --show-cursor --no-pager | sed -n 's/^-- cursor: //p' > /var/tmp/t3s-cursor; }
runsc_pids() {
    local p e
    for p in /proc/[0-9]*; do
        e=$(readlink "$p/exe" 2>/dev/null) || continue
        case "$e" in /usr/libexec/qdistro/runsc/*) echo "${p#/proc/}" ;; esac
    done
}
tree_procs() { find "$1" -name cgroup.procs -exec cat {} + 2>/dev/null | sort -n; }
pinkey() {
    local h; h=$(sha512sum < "/proc/$1/exe" 2>/dev/null | cut -d' ' -f1)
    awk -F= -v h="$h" '$1 ~ /_sha512$/ && $2 == h { print $1; f=1 } END { if (!f) print "-" }' "$PIN"
}
wait_for() {   # wait_for <secs> <cmd...>: poll until cmd succeeds
    local n="$1"; shift
    for _ in $(seq 1 $((n * 4))); do "$@" && return 0; sleep 0.25; done
    return 1
}
unit_inactive() { [ "$(systemctl is-active "$UNIT")" != active ] && [ "$(systemctl is-active "$UNIT")" != activating ] \
                  && [ "$(systemctl is-active "$UNIT")" != deactivating ]; }
ctr_running() { [ "$(as_admin podman inspect --format '{{.State.Status}}' "$CTR" 2>/dev/null)" = running ]; }
records() { find /run/qdistro-tier3s-ctl /run/qdistro-tier3s -mindepth 1 -maxdepth 1 -regextype egrep -regex '.*/[0-9a-f]{32}' 2>/dev/null; }
assert_gone() {   # every trace of a tier3s launch is absent
    is "$1-records" "$(records | wc -l)" 0
    is "$1-scopes" "$(systemctl list-units --all --plain --no-legend 'qdistro-tier3s-*.scope' | grep -c .)" 0
    is "$1-container" "$(as_admin podman ps -a --filter label=qdistro_tier3s_token --format '{{.Names}}' | grep -c .)" 0
    is "$1-runsc-procs" "$(runsc_pids | wc -l)" 0
    # runsc keeps one shared, empty, read-only `null-netns` file in its root for
    # network=none (present since the A-i feasibility runs); nothing per container
    ls -la /run/qdistro-tier3s-runsc/1000
    is "$1-state-root-no-container-state" "$(find /run/qdistro-tier3s-runsc/1000 -mindepth 1 ! -name null-netns | grep -c .)" 0
}
check_installed() {   # <expect-file>
    local sha mode path got
    while read -r sha mode path; do
        [ -n "$path" ] || continue
        got="$(sha256sum < "$path" 2>/dev/null | cut -d' ' -f1)"
        is "sha256 $path" "$got" "$sha"
        is "owner+mode $path" "$(stat -c '%U:%G %a' "$path" 2>/dev/null)" "root:root $mode"
    done < "$1"
    for d in /usr/lib/qdistro /usr/lib/qdistro/tier3s /usr/lib/qdistro/tier3s/seccomp; do
        is "dir $d" "$(stat -c '%U:%G %a' "$d")" "root:root 755"
    done
    is tmpfiles-state-root "$(stat -c '%u:%g %a' /run/qdistro-tier3s-runsc/1000)" "1000:1000 700"
    is tmpfiles-ctl "$(stat -c '%U:%G %a' /run/qdistro-tier3s-ctl)" "root:root 700"
    is tmpfiles-launch-parent "$(stat -c '%U:%G %a' /run/qdistro-tier3s)" "root:root 755"
    is runsc-not-installed-by-installer "$(test -e /usr/libexec/qdistro/runsc && echo present || echo absent)" absent
    # the unit is installed and NEVER started yet
    is unit-fragment "$(systemctl show -p FragmentPath --value "$UNIT")" /etc/systemd/system/qdistro-tier3s-silo@.service
    is unit-load "$(systemctl show -p LoadState --value "$UNIT")" loaded
    is unit-never-started "$(systemctl show -p ExecMainStartTimestampMonotonic --value "$UNIT")" 0
    is unit-inactive "$(systemctl is-active "$UNIT")" inactive
    # the running daemon serves this branch's code: its method is on the bus, and
    # it started after the installed file was written
    if busctl introspect org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
            | grep -q '^\.CreateTier3sSilo  *method  *ssss'; then ok daemon-serves-CreateTier3sSilo
    else bad daemon-serves-CreateTier3sSilo; fi
    local pid since mt
    pid="$(systemctl show -p MainPID --value qdistro-session-manager.service)"
    since="$(stat -c %Y "/proc/$pid")"; mt="$(stat -c %Y /usr/libexec/qdistro/qdistro_session_manager.py)"
    if [ "$pid" -gt 0 ] && [ "$since" -ge "$mt" ]; then ok daemon-started-after-install "pid $pid $since >= $mt"
    else bad daemon-started-after-install "pid $pid since=$since mtime=$mt"; fi
}

case "${1:-}" in
installed)
    check_installed "$2"; finish ;;
reinstall)
    out="$(bash scripts/install/install-session-manager.sh "$SRC/session_manager" 2>&1)"; rc=$?
    printf '%s\n' "$out" | tail -5
    is installer-rc "$rc" 0
    check_installed "$2"; finish ;;
provision)
    rel=$(sed -n 's/^release=//p' "$PIN"); want=$(sed -n 's/^tarball_sha512=//p' "$PIN")
    tb=/var/cache/qdistro/runsc/$rel/gvisor.tar.zstd
    is tarball-sha512 "$(sha512sum < "$tb" | cut -d' ' -f1)" "$want"
    tier3s/provision-runsc.sh --offline --cache-dir /var/cache/qdistro/runsc; is provision-rc $? 0
    # the probe the spawn runs: the INSTALLED copy, as root
    out="$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1)"; rc=$?
    printf '%s\n' "$out"
    is probe-rc "$rc" 0
    is probe-state-root "$(printf '%s\n' "$out" | grep -c '^PASS state_root')" 1
    finish ;;
image)
    rm -rf /var/tmp/t3s-img && mkdir -p /var/tmp/t3s-img/src && cp -a "$SRC"/. /var/tmp/t3s-img/src/
    chown -R admin:users /var/tmp/t3s-img
    as_admin env TMPDIR=/var/tmp bash -c 'cd /var/tmp/t3s-img/src && bash tier3s/make-tier3s-image.sh headless-smoke' \
        > /var/tmp/t3s-img/build.log 2>&1; rc=$?
    tail -8 /var/tmp/t3s-img/build.log
    is build-rc "$rc" 0
    pin=$(sed -n 's/^snapshot=\([0-9]\{8\}\)$/\1/p' snapshot.conf | head -1)
    is image-snapshot-label "$(as_admin podman image inspect --format '{{index .Labels "org.qdistro.snapshot"}}' \
        localhost/qdistro/tier3s-headless-smoke:latest)" "$pin"
    echo "IMAGE_ID=$(as_admin podman image inspect --format '{{.Id}}' localhost/qdistro/tier3s-headless-smoke:latest)"
    finish ;;
rule)
    chk() { as_admin busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
            org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' "$ACTION" 0 2>&1; }
    is installed-broker-has-prefix "$(grep -c '"qdistro.tier3s.spawn:",' /usr/libexec/qdistro/qdistro_admin_broker.py)" 1
    is check-without-rule "$(chk)" 's "unknown"'
    cat > /etc/qdistro/rules.d/50-tier3s-smoke.yaml <<YAML
- name: tier3s-a-ii-smoke
  decision: allow
  match:
    uid: 1000
    action: "$ACTION"
YAML
    chmod 0644 /etc/qdistro/rules.d/50-tier3s-smoke.yaml
    wait_for 15 bash -c "[ \"\$(runuser -u admin -- busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' '$ACTION' 0)\" = 's \"allow\"' ]"
    is check-with-rule "$(chk)" 's "allow"'
    finish ;;
smoke-exit)
    echo "## CreateTier3sSilo + StartSilo, default argv: the smoke runs, exits 0, the launch tears itself down"
    is silo-absent-before "$(silo_state)" absent
    sm CreateTier3sSilo ssss smoke headless-smoke smoke none; is create-rc $? 0
    is silo-created "$(silo_state)" Created
    out="$(sm FreezeSilo s smoke 2>&1)"; echo "$out"
    is freeze-on-created-refused "$(printf '%s' "$out" | grep -c 'unsupported for tier3s')" 1
    mark_cursor
    sm StartSilo s smoke; is start-rc $? 0
    wait_for 120 unit_inactive; is unit-inactive-after-exit "$(systemctl is-active "$UNIT")" inactive
    unit_log
    tok="$(unit_log | sed -n 's/^LAUNCH_TOKEN=\([0-9a-f]\{32\}\)$/\1/p' | head -1)"
    echo "token=$tok"; scope_log "$tok"
    # a workload this short may exit before the spawn's 0.25 s poll records it running;
    # the smoke output below is what shows it ran under gVisor
    echo "INFO recorded-running: $(unit_log | grep -c "spawn-tier3s: running: $CTR sentry=")"
    is unit-result "$(systemctl show -p Result --value "$UNIT")" success
    is main-status "$(systemctl show -p ExecMainStatus --value "$UNIT")" 0
    # podman's attached stdout and conmon's journald log driver may both carry a line
    is smoke-done "$(scope_log "$tok" | grep -q '^SMOKE done' && echo yes || echo no)" yes
    is smoke-gvisor-kernel "$(scope_log "$tok" | grep '^SMOKE dmesg=' | grep -q gVisor && echo yes || echo no)" yes
    is torn-down "$(unit_log | grep -c "qdistro-tier3s-cleanup: [0-9a-f]\{32\}: torn down ($CTR)")" 1
    is stanza-token-in-record-line "$(unit_log | grep -c "LAUNCH_TOKEN=$(sed -n "s/^TIER3S_LAUNCH_TOKEN='\{0,1\}\([0-9a-f]\{32\}\).*/\1/p" /run/qdistro/silo-launch/smoke.env)")" 1
    sm StopSilo si smoke 10; is stop-rc $? 0
    is silo-stopped "$(silo_state)" Stopped
    is stanza-removed "$(test -e /run/qdistro/silo-launch/smoke.env && echo present || echo absent)" absent
    assert_gone after-exit
    finish ;;
smoke-live)
    echo "## argv --hold via silos.yaml (manager restart), StartSilo, live launch checks, StopSilo"
    systemctl stop qdistro-session-manager.service
    python3 - <<'PY'
import re, pathlib
p = pathlib.Path("/etc/qdistro/silos.yaml")
s = p.read_text()
new, n = re.subn(r'(\n\s+argv: )\[[^\n]*\]', r'\1["qdistro-tier3s-smoke", "--hold", "600"]', s, count=1)
assert n == 1, s
p.write_text(new)
print(new)
PY
    systemctl start qdistro-session-manager.service
    wait_for 30 bash -c 'busctl list --no-pager | grep -q org.qdistro.SessionManager1'
    is silo-stopped-after-restart "$(silo_state)" Stopped
    mark_cursor
    sm StartSilo s smoke; is start-rc $? 0
    wait_for 120 ctr_running; is container-running "$(as_admin podman inspect --format '{{.State.Status}}' "$CTR")" running
    wait_for 30 bash -c "journalctl -u '$UNIT' --no-pager -o cat --after-cursor='$(cat /var/tmp/t3s-cursor)' | grep -q 'spawn-tier3s: running: '"
    is silo-active "$(silo_state)" Active
    tok="$(records | sed -n 's#^/run/qdistro-tier3s-ctl/##p' | head -1)"
    echo "token=$tok"; cat "/run/qdistro-tier3s-ctl/$tok/state"
    st() { sed -n "s/^$1=//p" "/run/qdistro-tier3s-ctl/$tok/state"; }
    is record-unit "$(st unit)" "$UNIT"
    is record-container "$(st container)" "$CTR"
    is record-phase "$(st phase)" running
    is record-token-is-the-stanza-token "$tok" "$(as_admin podman inspect --format '{{index .Config.Labels "qdistro_tier3s_token"}}' "$CTR")"
    is runtime "$(as_admin podman inspect --format '{{.OCIRuntime}}' "$CTR")" /usr/libexec/qdistro/tier3s-runsc
    scope="$(st scope_unit)"; is scope-active "$(systemctl is-active "$scope")" active
    cg="/sys/fs/cgroup$(st scope_cgroup)"
    n_in=0; n_out=0
    for p in $(runsc_pids); do
        if tree_procs "$cg" | grep -qx "$p"; then n_in=$((n_in + 1)); else n_out=$((n_out + 1)); fi
    done
    is runsc-procs-outside-scope "$n_out" 0
    if [ "$n_in" -ge 3 ]; then ok runsc-procs-in-scope "$n_in"; else bad runsc-procs-in-scope "$n_in"; fi
    is sentry-exe-pin "$(pinkey "$(st sentry_pid)")" sidecar_gvisor_sentry_sha512
    out="$(sm FreezeSilo s smoke 2>&1)"; echo "$out"
    is freeze-refused "$(printf '%s' "$out" | grep -c 'freeze/resume is unsupported for tier3s silos')" 1
    is still-active-after-freeze "$(silo_state)" Active
    sm StopSilo si smoke 10; is stop-rc $? 0
    is silo-stopped "$(silo_state)" Stopped
    unit_log | tail -25; scope_log "$tok"
    is smoke-term "$(scope_log "$tok" | grep -q '^SMOKE term' && echo yes || echo no)" yes
    is torn-down "$(unit_log | grep -c "qdistro-tier3s-cleanup: $tok: torn down ($CTR)")" 1
    is unit-result "$(systemctl show -p Result --value "$UNIT")" success
    is scope-gone "$(systemctl is-active "$scope")" inactive
    assert_gone after-stop
    finish ;;
final)
    sm DeleteSilo s smoke; is delete-rc $? 0
    is silo-deleted "$(silo_state)" absent
    rm -f /etc/qdistro/rules.d/50-tier3s-smoke.yaml
    assert_gone final
    finish ;;
*) echo "usage: $0 installed|reinstall <expect>|provision|image|rule|smoke-exit|smoke-live|final" >&2; exit 2 ;;
esac
