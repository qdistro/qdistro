# tier3s-guest-lib.sh — GUEST side (root, qci bats worker or dev test VM).
# Shared by tier3s-guest-setup.sh and the tier 3s drivers s120-s122
# (todo/paravirt 06 "Δ DONE bar"; tier3s/CONTRACT.md). Sourced, never run.
#
# Every check prints exactly ONE line: `PASS: <name> (<detail>)` or
# `FAIL: <name>: <reason>`. `finish` prints `[<tag>] N passes, M failures`
# and exits 1 when M > 0. A driver that dies before `finish` prints no
# summary line, which the bats wrapper treats as a failure (the EXIT trap
# also prints `FAIL: <tag> exited early`). INFO lines are corroboration or
# diagnostics, never a verdict.
#
# Everything here reads the INSTALLED tier 3s files (/usr/lib/qdistro/tier3s,
# /usr/libexec/qdistro), never a checkout: the drivers test what the
# installer put in place.
set -u
T3S_PASS=0
T3S_FAIL=0
T3S_TAG=${T3S_TAG:-t3s}
T3S_DONE=0
PIN=/usr/lib/qdistro/tier3s/RUNSC_RELEASE
CLEANUP=/usr/libexec/qdistro/qdistro-tier3s-cleanup
WRAPPER=/usr/libexec/qdistro/tier3s-runsc
CTL=/run/qdistro-tier3s-ctl
LAUNCHES=/run/qdistro-tier3s
STANZA_DIR=/run/qdistro/tier3s-launch
SROOT=/run/qdistro-tier3s-runsc/1000
IMAGE=localhost/qdistro/tier3s-headless-smoke:latest
SMOKE_APP=qdistro-tier3s-smoke
ACTION="qdistro.tier3s.spawn:headless-smoke/$SMOKE_APP"
RULE_FILE=/etc/qdistro/rules.d/50-tier3s-qci.yaml
WORK=/var/tmp/t3s-drv
mkdir -p "$WORK"

pass() { echo "PASS: $*"; T3S_PASS=$((T3S_PASS + 1)); }
# qry <cmd...>: run a query for an ABSENCE oracle. Its output, or the single
# line `QUERY-FAILED(<rc>): <cmd>` when it fails, so a count over it can never
# read a failed query as "nothing there" (sol A-iii r1 P2).
qry() {
    local out rc
    out="$("$@" 2>/dev/null)"; rc=$?
    if [ "$rc" -ne 0 ]; then echo "QUERY-FAILED($rc): $*"; return 1; fi
    [ -z "$out" ] || printf '%s\n' "$out"
}
fail() { echo "FAIL: $*"; T3S_FAIL=$((T3S_FAIL + 1)); }
info() { echo "INFO: $*"; }
step() { printf '\n## %s\n' "$*"; }
is() {   # is <name> <got> <want>; an empty <want> is never a match
    if [ -z "$3" ]; then fail "$1: nothing to compare against (empty expected value; got '$2')"
    elif [ "$2" = "$3" ]; then pass "$1 ($2)"; else fail "$1: got '$2', want '$3'"; fi
}
yes_no() { if "$@"; then echo yes; else echo no; fi; }
finish() {
    T3S_DONE=1
    echo "[$T3S_TAG] $T3S_PASS passes, $T3S_FAIL failures"
    [ "$T3S_FAIL" -eq 0 ]
    exit $?
}
_t3s_exit_trap() {
    local rc=$?
    [ -z "${T3S_EXIT_HOOK:-}" ] || eval "$T3S_EXIT_HOOK"
    if [ "$T3S_DONE" = 0 ]; then
        echo "FAIL: $T3S_TAG exited early (rc=$rc) before its summary"
        echo "[$T3S_TAG] $T3S_PASS passes, $((T3S_FAIL + 1)) failures"
        exit 1
    fi
}
trap _t3s_exit_trap EXIT

as_admin() {   # the same scrubbed admin environment the spawn and cleanup use
    runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin LOGNAME=admin \
        XDG_RUNTIME_DIR=/run/user/1000 "$@"
}
pm() { as_admin podman "$@"; }   # PLAIN podman: no --runtime, no --root, no runtime flags
# StartSilo of a tier3s silo returns only once the launch runs (the unit is
# Type=notify), so the call gets more than busctl's default 25 s: the start
# path can hold the manager up to ~255 s in the worst case (CONTRACT §6)
sm() {
    as_admin busctl --system --timeout=300 call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
        org.qdistro.SessionManager1 "$@"
}
broker_check() {   # broker_check <action> -> allow|deny|unknown|ERR
    local out
    out="$(as_admin busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' "$1" 0 2>&1)" || { echo ERR; return; }
    out="${out#s \"}"; echo "${out%\"}"
}
# busctl's JSON output, not its text form: the text form escapes more than
# `"` (an observed_reason with a `'` broke the old sed-based decoding)
silo_state() {   # silo_state <name> -> its ListSilos state, or "absent" (QUERY-FAILED on a failed call)
    as_admin busctl --system --timeout=300 --json=short call org.qdistro.SessionManager1 \
        /org/qdistro/SessionManager1 org.qdistro.SessionManager1 ListSilos | python3 -c '
import json, sys
try:
    rows = json.loads(json.load(sys.stdin)["data"][0])
except Exception:
    print("QUERY-FAILED"); sys.exit(0)
for s in rows:
    if s["name"] == sys.argv[1]: print(s["state"]); break
else: print("absent")' "$1"
}
wait_for() {   # wait_for <secs> <cmd...>
    local n="$1"; shift
    # a wedged probe call (a stalled podman/busctl IPC) must fail the
    # iteration, not hang the driver past vm-exec's 1800 s bound — every
    # probe in this lib is a sub-second query, 30 s is generous.
    for _ in $(seq 1 $((n * 4))); do timeout 30 "$@" && return 0; sleep 0.25; done
    return 1
}
unit_state() { systemctl show -p ActiveState --value "$1" 2>/dev/null; }
# a failed `systemctl show` (empty answer) is NOT "down" (sol A-iii r1 P2)
unit_down() { case "$(unit_state "$1")" in inactive|failed) return 0 ;; esac; return 1; }
unit_of() { echo "qdistro-tier3s-silo@$1.service"; }
ctr_of() { echo "qdistro-tier3s-$1"; }
ctr_status() { pm inspect --format '{{.State.Status}}' "$1" 2>/dev/null; }
ctr_running() { [ "$(ctr_status "$1")" = running ]; }
manager_up() { busctl --system list --no-pager 2>/dev/null | grep -q '^org\.qdistro\.SessionManager1 '; }
journal_cursor() { journalctl -n 0 --show-cursor --no-pager 2>/dev/null | sed -n 's/^-- cursor: //p'; }
# Journal of one unit since a cursor; scoped to the unit, never the whole
# journal (qemu-ga logs the guest-exec command text, which carries our markers).
unit_log() { journalctl -u "$1" --no-pager -o cat --after-cursor="$2" 2>/dev/null; }
# The workload's output: journald files it under the owning scope (podman's
# attached stdout and conmon's log driver run there), not under the launch unit.
scope_log() { journalctl _SYSTEMD_UNIT="qdistro-tier3s-$1.scope" --no-pager -o cat 2>/dev/null; }
# Units systemd (pid 1) started since a cursor whose UNIT field matches a
# regex: journal fields, never the message text of other processes.
# A failed query prints QUERY-FAILED instead of a count (sol A-iii r1 P2).
units_started_since() { units_jobs_since "$1" start "$2"; }   # <cursor> <python regex>
# Jobs of a type that systemd COMPLETED since a cursor (JOB_TYPE=<type>,
# JOB_RESULT=done for start/stop... "Stopped"/"Started" messages), or, for
# start, the "Starting" messages too (the historic count of units_started_since).
units_jobs_since() {   # units_jobs_since <cursor> <start|stop> <python regex>
    qry journalctl --after-cursor="$1" _PID=1 -o json --no-pager | python3 -c '
import json, re, sys
n = 0
for line in sys.stdin:
    if line.startswith("QUERY-FAILED"):
        print("QUERY-FAILED"); sys.exit(0)
    j = json.loads(line)
    if j.get("JOB_TYPE") != sys.argv[1] or not re.fullmatch(sys.argv[2], j.get("UNIT", "")):
        continue
    if sys.argv[1] == "start" or j.get("JOB_RESULT") == "done":
        n += 1
print(n)' "$2" "$3"
}
T3S_SCOPE_RE='qdistro-tier3s-[0-9a-f]{32}\.scope'
# no fallback tier: tier-2 silo/podapp units and tier-3 user-silo sessions
FALLBACK_RE='(qdistro-tier2-.*|qdistro-podapp@.*|qdshell-session.*|qdistro-silo-launch.*)\.(service|scope)'
# admin's podman container events since a time, minus the probe's own scratch
# container (probe.sh creates and removes tier3s-probe-<pid> to check the
# runtime; it is never started)
launch_events_since() {   # launch_events_since <iso time>; a failed query yields a QUERY-FAILED line
    qry pm events --since "$1" --until "$(date --iso-8601=seconds)" --filter type=container \
        --format '{{.Status}} {{.Name}}' | grep -vE '^(create|remove) tier3s-probe-[0-9]+$' | grep .
}
# control-record tokens; a failed find yields a QUERY-FAILED line (sol A-iii r2 P2)
records() { qry find "$CTL" -mindepth 1 -maxdepth 1 -regextype egrep -regex '.*/[0-9a-f]{32}' -printf '%f\n'; }
rec() { sed -n "s/^$2=//p" "$CTL/$1/state" 2>/dev/null; }   # rec <token> <key>
token_of_unit() {   # the control record whose unit= is $1 (exactly one)
    local t
    for t in $(records); do [ "$(rec "$t" unit)" = "$1" ] && echo "$t"; done
}
starttime() { local s; { read -r s < "/proc/$1/stat"; } 2>/dev/null || return 1; s="${s##*) }"; set -- $s; echo "${20}"; }
tree_procs() { find "$1" -name cgroup.procs -exec cat {} + 2>/dev/null | sort -n; }
runsc_pids() {
    local p e
    for p in /proc/[0-9]*; do
        e=$(readlink "$p/exe" 2>/dev/null) || continue
        case "$e" in /usr/libexec/qdistro/runsc/*) echo "${p#/proc/}" ;; esac
    done
}
pin_value() { sed -n "s/^$1=//p" "$PIN"; }
exe_sha512() { sha512sum < "/proc/$1/exe" 2>/dev/null | cut -d' ' -f1; }

# A rule file allowing (or denying) the smoke spawn for admin; waits until the
# broker answers it. allow_rule <allow|deny|none>
set_rule() {
    case "$1" in
        none) rm -f "$RULE_FILE" ;;
        allow|deny)
            cat > "$RULE_FILE" <<YAML
# test-authored by the tier 3s qci drivers (tests/integration/vm/tier3s-*)
- name: tier3s-qci-$1
  decision: $1
  match:
    uid: 1000
    action: "$ACTION"
YAML
            chmod 0644 "$RULE_FILE" ;;
    esac
    local want="$1"; [ "$want" = none ] && want=unknown
    wait_for 20 bash -c "[ \"\$(runuser -u admin -- busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' '$ACTION' 0 2>/dev/null)\" = 's \"$want\"' ]"
}

# Set the launch argv of tier3s silo rows in /etc/qdistro/silos.yaml. The
# manager rewrites that file, so it is edited with the manager STOPPED, then
# started again (its startup reconciliation stops any live tier3s launch, so
# call this only when none should be running). set_argv <silo>=<spec>...,
# spec = default | <hold seconds>.
set_argv() {
    local rc
    systemctl stop qdistro-session-manager.service
    python3 - "$@" <<'PY'
import re, sys, pathlib
p = pathlib.Path("/etc/qdistro/silos.yaml")
s = p.read_text()
for arg in sys.argv[1:]:
    name, spec = arg.split("=", 1)
    argv = '[]' if spec == "default" else f'["qdistro-tier3s-smoke", "--hold", "{spec}"]'
    pat = re.compile(r'(\n  - name: ' + re.escape(name) + r'\n(?:    [^\n]*\n)*?      argv: )\[[^\n]*\]')
    s, n = pat.subn(lambda m: m.group(1) + argv, s)
    if n != 1:
        sys.exit(f"silos.yaml: no argv for silo {name}")
    print(f"silos.yaml: {name} argv -> {argv}")
p.write_text(s)
PY
    rc=$?
    systemctl start qdistro-session-manager.service
    wait_for 30 manager_up
    return $rc
}

# Write a launch stanza by hand (what the manager writes at StartSilo; root
# 0600) so a launch unit can be started WITHOUT the manager. Prints the token.
write_stanza() {   # write_stanza <silo> <argv json>
    write_stanza_workload "$1" headless-smoke "$2"
}
# Phase B variant: any workload (GUI drivers refuse-test weston-terminal /
# foot launches this way). write_stanza_workload <silo> <workload> <argv json>
write_stanza_workload() {
    local tok
    tok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    install -d -m 0700 "$STANZA_DIR"
    ( umask 077
      printf '%s\n' "TIER3S_SILO=$1" "TIER3S_BINDING=$1" "TIER3S_WORKLOAD=$2" "TIER3S_NETWORK=none" \
          "TIER3S_LAUNCH_TOKEN=$tok" "TIER3S_ARGV_JSON='$3'" > "$STANZA_DIR/$1.env" )
    chmod 0600 "$STANZA_DIR/$1.env"
    echo "$tok"
}

# Snapshot every process of a live launch (recursive cgroup.procs of its
# scope) as "pid starttime" lines, so absence can later be asserted per
# process, not by name or uid.
snapshot_launch() {   # snapshot_launch <token> -> $WORK/<token>.procs
    local cg p st
    cg="/sys/fs/cgroup$(rec "$1" scope_cgroup)"
    echo "$cg" > "$WORK/$1.cg"
    : > "$WORK/$1.procs"
    for p in $(tree_procs "$cg"); do
        st=$(starttime "$p") && echo "$p $st" >> "$WORK/$1.procs"
    done
    pm inspect --format '{{.Id}}' "$(rec "$1" container)" > "$WORK/$1.id" 2>/dev/null
}

# Bring a silo up live (argv must already be --hold) and wait until the spawn
# recorded it running. up_silo <silo> -> prints the token on success
up_silo() {
    local s="$1" unit tok cur
    unit=$(unit_of "$s"); cur=$(journal_cursor)
    sm StartSilo s "$s" > /dev/null || { echo ""; return 1; }
    # Type=notify: StartSilo returns only once the launch is recorded running
    { tok=$(token_of_unit "$unit" | head -1); echo "${tok:+$(rec "$tok" phase)}"; } > "$WORK/up-phase.$s"
    if ! wait_for 120 bash -c "journalctl -u '$unit' --no-pager -o cat --after-cursor='$cur' | grep -q 'spawn-tier3s: running: '"; then
        unit_log "$unit" "$cur" | tail -20 >&2; echo ""; return 1
    fi
    tok=$(token_of_unit "$unit")
    [ -n "$tok" ] || { echo ""; return 1; }
    snapshot_launch "$tok"
    # the smoke installs its SIGTERM handler only when it reaches --hold: act
    # on the launch only after it says so (an earlier SIGTERM kills sh outright)
    if ! wait_for 60 bash -c "journalctl _SYSTEMD_UNIT=qdistro-tier3s-$tok.scope --no-pager -o cat | grep -q '^SMOKE holding'"; then
        echo "up_silo: $s never reached 'SMOKE holding'" >&2; echo ""; return 1
    fi
    echo "$tok"
}

# A relaunch counts only when the NEW token's record is phase=running, its
# container runs with that token label and its owning scope is live (astra A
# r1 #6): a freshly published phase=created record is not a running launch.
# assert_relaunched <tag> <silo> <old token> -> prints nothing; PASS/FAIL lines
assert_relaunched() {
    local tag="$1" s="$2" old="$3" u t=""
    u=$(unit_of "$s")
    for _ in $(seq 1 360); do
        t=$(token_of_unit "$u" | head -1)
        [ -n "$t" ] && [ "$t" != "$old" ] && [ "$(rec "$t" phase)" = running ] && break
        sleep 0.25
    done
    if [ -n "$t" ] && [ "$t" != "$old" ]; then pass "$tag: relaunched with a fresh token ($t, was $old)"
    else fail "$tag: no fresh token (record '${t:-none}', old $old)"; return; fi
    is "$tag: the new launch is recorded running" "$(rec "$t" phase)" running
    is "$tag: its container runs under the new token" \
        "$(ctr_status "$(ctr_of "$s")"):$(pm inspect --format '{{index .Config.Labels "qdistro_tier3s_token"}}' "$(ctr_of "$s")" 2>/dev/null)" \
        "running:$t"
    is "$tag: its owning scope is live" "$(unit_state "qdistro-tier3s-$t.scope")" active
    echo "$t" > "$WORK/relaunched.$s"
}

# Eventual absence of everything one launch owned (DONE bar 2). Each item is
# its own PASS/FAIL line. assert_launch_gone <tag> <token> <container> [secs]
assert_launch_gone() {
    local tag="$1" tok="$2" ctr="$3" secs="${4:-60}" scope left p st rc id cg
    scope="qdistro-tier3s-$tok.scope"
    procs_left() {
        local n=0
        while read -r p st; do
            [ -n "$p" ] || continue
            [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && n=$((n + 1))
        done < "$WORK/$tok.procs"
        [ "$n" -eq 0 ]
    }
    wait_for "$secs" procs_left
    left=0
    while read -r p st; do
        [ -n "$p" ] || continue
        [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && left=$((left + 1))
    done < "$WORK/$tok.procs"
    if [ ! -s "$WORK/$tok.procs" ]; then fail "$tag: no process snapshot for $tok (cannot prove absence)"
    elif [ "$left" -eq 0 ]; then pass "$tag: all $(wc -l < "$WORK/$tok.procs") launch processes gone (pid+starttime)"
    else fail "$tag: $left of $(wc -l < "$WORK/$tok.procs") launch processes still alive"; fi
    wait_for "$secs" unit_down "$scope"
    cg=$(cat "$WORK/$tok.cg" 2>/dev/null)
    if unit_down "$scope" && [ -n "$cg" ] && [ ! -d "$cg" ]; then pass "$tag: scope $scope gone (unit down, $cg absent)"
    else fail "$tag: scope $scope still $(unit_state "$scope")"; fi
    wait_for "$secs" test ! -e "$LAUNCHES/$tok"
    if [ ! -e "$LAUNCHES/$tok" ]; then pass "$tag: per-launch dir $LAUNCHES/$tok gone"
    else fail "$tag: $LAUNCHES/$tok still present"; fi
    wait_for "$secs" test ! -e "$CTL/$tok"
    if [ ! -e "$CTL/$tok" ]; then pass "$tag: control record $CTL/$tok gone"
    else fail "$tag: control record $CTL/$tok still present: $(tr '\n' ' ' < "$CTL/$tok/state" 2>/dev/null)"; fi
    wait_for "$secs" bash -c "! runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 podman container exists '$ctr'"
    pm container exists "$ctr"; rc=$?
    case "$rc" in
        1) pass "$tag: container $ctr gone (podman exists rc=1)" ;;
        0) fail "$tag: container $ctr still exists ($(ctr_status "$ctr"))" ;;
        *) fail "$tag: podman query failed (rc=$rc), absence NOT proven" ;;
    esac
    id=$(cat "$WORK/$tok.id" 2>/dev/null)
    if [ -z "$id" ]; then fail "$tag: no container id captured for $tok"
    elif [ -z "$(qry find "$SROOT" -mindepth 1 -name "*$id*")" ]; then pass "$tag: no runsc state for ${id:0:12} in $SROOT"
    else fail "$tag: runsc state for ${id:0:12} left in $SROOT: $(find "$SROOT" -mindepth 1 -name "*$id*" | tr '\n' ' ')"; fi
}

# Nothing tier 3s is running at all (end of a driver / between sections).
assert_all_clear() {   # assert_all_clear <tag>
    is "$1: control records" "$(records | wc -l)" 0
    is "$1: scopes" "$(qry systemctl list-units --all --plain --no-legend 'qdistro-tier3s-*.scope' | grep -c .)" 0
    is "$1: labelled containers" "$(qry pm ps -a --filter label=qdistro_tier3s_token --format '{{.Names}}' | grep -c .)" 0
    is "$1: runsc-bundle processes" "$(runsc_pids | wc -l)" 0
    # runsc keeps one shared, empty, read-only null-netns file for network=none
    is "$1: state root holds no container state" "$(qry find "$SROOT" -mindepth 1 ! -name null-netns | grep -c .)" 0
    # the cleanup's per-call scopes and work dirs end with each call / run (astra A r2 #2)
    is "$1: no cleanup call scope left" "$(qry systemctl list-units --all --plain --no-legend 'qdistro-t3s-call-*.scope' | grep -c .)" 0
    # a cleanup run's private .call-* dir can outlive its triggering call by a
    # beat (the dir is removed at the end of that run, not before the reply)
    wait_for 15 bash -c "[ -z \"\$(find '$CTL' -mindepth 1 -maxdepth 1 -name '.call-*' -print -quit 2>/dev/null)\" ]" || :
    # a SIGKILLed run's .call-* dir is swept by the NEXT --reap-stale, not by
    # a clock — invoke the designed sweep so a pending-sweep dir does not
    # read as a leftover. Dirs surviving it have live/undecidable owners and
    # still fail the count.
    "$CLEANUP" --reap-stale >/dev/null 2>&1 || :
    is "$1: no cleanup work dir left" "$(qry find "$CTL" -mindepth 1 -maxdepth 1 -name '.call-*' | grep -c .)" 0
}

# ===========================================================================
# Phase B (ΔB7-ΔB9): GUI/waypipe helpers for s123-s129. Everything below is
# additive; the Phase A helpers above are unchanged.
#
# A GUI launch adds, on top of the Phase A topology (CONTRACT §5 step 12):
#   - the bridge pair in the LAUNCH UNIT's cgroup (not the scope):
#     spawn -> runuser(uid0->admin) -> qdistro-secctx-exec -> waypipe client;
#   - the bridge socket $LAUNCHES/<token>/link.sock (bind-mounted into the
#     sandbox at /run/qdistro/link);
#   - the secctx listener /run/user/1000/wayland-secctx-<w>-<i> that the inner
#     waypipe client connects to (its WAYLAND_DISPLAY);
#   - the launch record /run/user/1000/qdistro-tier3s-launchrec-<file-id>.pid
#     (removed by the spawn right after RegisterLaunch);
#   - the broker RegisterLaunch binding the bridge client pid to
#     (bare silo, qdistro.tier3s, qdistro.tier3s.<silo>, token).
#
# The bridge pair is deliberately NOT in the scope's cgroup, so
# snapshot_launch/assert_launch_gone do not see it; GUI drivers snapshot it
# with snapshot_bridge and assert its absence with assert_bridge_gone.

ADMIN_RT=/run/user/1000
GUI_DISPLAY=wayland-1
GUI_ENGINE=qdistro.tier3s

# --- session / journal evidence --------------------------------------------

comp_pid() {   # the running compositor's MainPID, or empty
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user show qdwin-compositor.service -p MainPID --value 2>/dev/null
}
# qdshell and the compositor are admin --user units: their stdout lands in the
# journal with the _SYSTEMD_USER_UNIT field. Scope every grep to the producing
# unit (qemu-ga logs the guest-exec command TEXT; a whole-journal grep for a
# marker that appears in a command line would self-match).
qdshell_log() { journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat ${1:+--after-cursor="$1"} 2>/dev/null; }
comp_log()    { journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service -b --no-pager -o cat ${1:+--after-cursor="$1"} 2>/dev/null; }
broker_log()  { journalctl -u qdistro-admin-broker.service --no-pager -o cat ${1:+--after-cursor="$1"} 2>/dev/null; }

# qs_ipc <target> <method> [args...] — Quickshell IPC as admin. `qs ipc`
# filters by display connection by default and the runuser session has no
# wayland of its own: --any-display bypasses the filter (s48's convention);
# -p selects the qdshell instance.
qs_ipc() {
    as_admin qs -p /usr/share/quickshell/qdshell ipc --any-display call "$@" 2>&1 | head -1
}

# The toplevel handle qdshell recorded for a tier3s silo, from its own
# journal line "[tier3s] toplevel observed silo=<s> ... handle=<N>". Empty
# when none was logged (since the optional cursor).
t3s_window_handle() {   # t3s_window_handle <silo> [cursor]
    qdshell_log "${2:-}" | sed -n "s/.*\[tier3s\] toplevel observed silo=$1 .*handle=\([0-9]\{1,\}\).*/\1/p" | tail -1
}
# compositor-side handle for a secctx app_id (toplevel_added line).
comp_toplevel() {   # comp_toplevel <app_id> [cursor] -> "handle=N pid=P title=..."
    comp_log "${2:-}" | grep "toplevel_added .* app_id=$1 " | tail -1
}

# --- argv / rules -----------------------------------------------------------

# set_argv_json <silo>=<argv-json> ... — like set_argv but takes the raw JSON
# argv (GUI drivers need argv other than the smoke's --hold; e.g. a foot
# command argv for the OSC-52 clipboard source). Same discipline: the manager
# rewrites /etc/qdistro/silos.yaml, so edit it with the manager stopped.
set_argv_json() {
    local rc
    systemctl stop qdistro-session-manager.service
    python3 - "$@" <<'PY'
import json, re, sys, pathlib
p = pathlib.Path("/etc/qdistro/silos.yaml")
s = p.read_text()
for arg in sys.argv[1:]:
    name, spec = arg.split("=", 1)
    if spec == "default":
        argv = '[]'
    else:
        argv = json.dumps(json.loads(spec))   # validate + normalize
    pat = re.compile(r'(\n  - name: ' + re.escape(name) + r'\n(?:    [^\n]*\n)*?      argv: )\[[^\n]*\]')
    s, n = pat.subn(lambda m: m.group(1) + argv, s)
    if n != 1:
        sys.exit(f"silos.yaml: no argv for silo {name}")
    print(f"silos.yaml: {name} argv -> {argv}")
p.write_text(s)
PY
    rc=$?
    systemctl start qdistro-session-manager.service
    wait_for 30 manager_up
    return $rc
}

# set_rules <verdict:action> ... | set_rules none — write the shared test rule
# file with one entry per <verdict:action> pair (GUI drivers need the spawn
# action AND, in the clipboard drivers, the transfer action, with independent
# verdicts). Waits until the broker answers the FIRST action with its verdict.
# `none` removes the file entirely (the broker goes back to unknown/deny).
set_rules() {
    [ "$1" = none ] && { rm -f "$RULE_FILE"; return; }
    {
        echo "# test-authored by the tier 3s qci drivers (tests/integration/vm/tier3s-*)"
        local spec verdict a
        for spec in "$@"; do
            verdict="${spec%%:*}"; a="${spec#*:}"
            printf '%s\n' "- name: tier3s-qci-$verdict" \
                "  decision: $verdict" "  match:" "    uid: 1000" \
                "    action: \"$a\""
        done
    } > "$RULE_FILE"
    chmod 0644 "$RULE_FILE"
    local want="${1%%:*}" probe="${1#*:}"
    wait_for 20 bash -c "[ \"\$(runuser -u admin -- busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' '$probe' 0 2>/dev/null)\" = 's \"$want\"' ]"
}

# save_rule <filename.yaml> <yaml-body> — the admin control-plane SaveRule
# (root dbus-send, argv names the method → trusted admin-control helper).
# Prints the saved path. save_rule_rc <...>: same but prints nothing and
# returns the dbus-send rc.
save_rule() {
    local out
    out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.SaveRule \
        "string:$1" "string:$2" 2>&1) || { echo "ERR: $out"; return 1; }
    printf '%s\n' "$out" | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g'
}
delete_rule() {   # delete_rule <filename.yaml>
    dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.DeleteRule \
        "string:$1" > /dev/null 2>&1
}

# t3s_clip_source <silo> <mime> <text> [helper args...] — run
# qdistro-test-clipboard-source as admin under the SAME wrap the waypipe
# bridge client uses (runuser -> env -i -> QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER
# -> qdistro-secctx-exec tagged qdistro.tier3s.<silo>). The exec chain
# preserves the pid, so the caller's $! IS the tagged source process;
# kill it to drop the selection. Prints the instance-id on stdout so the
# caller can register exactly the tag it launched. Extra args pass to the
# helper (--toplevel, --emit-interval, --title).
t3s_clip_source() {
    local silo="$1" mime="${2:-text/plain}" text="${3:-qdistro-t3s-clip}"
    shift $(( $# > 3 ? 3 : $# ))
    # A unique instance-id per call is REQUIRED: two live clients tagged
    # with the same instance-id both connect, but the second's
    # set_selection is silently swallowed by the tagged channel
    # (reproduced on the preserved s127 worker — the compositor logs no
    # selection_set for it). Real launches always get a unique token.
    # $BASHPID, not a counter: callers run this function backgrounded, so
    # a shell-variable increment would stay trapped in the subshell and
    # every call would reuse the same id.
    local inst="clipsrc-$silo-$BASHPID"
    printf 'INSTANCE=%s\n' "$inst"
    runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin \
        USER=admin LOGNAME=admin XDG_RUNTIME_DIR=/run/user/1000 \
        WAYLAND_DISPLAY=wayland-1 QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
        ${QDISTRO_CLIP_SRC_DELAY_MS:+QDISTRO_CLIP_SRC_DELAY_MS=$QDISTRO_CLIP_SRC_DELAY_MS} \
        ${QDISTRO_LAUNCH_RECORD_PATH:+QDISTRO_LAUNCH_RECORD_PATH=$QDISTRO_LAUNCH_RECORD_PATH} \
        ${QDISTRO_LAUNCH_RECORD_TOKEN:+QDISTRO_LAUNCH_RECORD_TOKEN=$QDISTRO_LAUNCH_RECORD_TOKEN} \
        qdistro-secctx-exec --sandbox-engine qdistro.tier3s \
            --app-id "qdistro.tier3s.$silo" --instance-id "$inst" \
            -- qdistro-test-clipboard-source --mime "$mime" --text "$text" "$@"
}

# register_clip_source <silo> <instance> <pid> — the SAME RegisterLaunch
# call the spawn path makes for the bridge client (root-only, broker
# re-verifies the live pid+starttime): it binds the tagged source's
# (pid,starttime) to qdistro.tier3s.<silo> in the launch-record store so
# the relayed source pid resolves under lineage_enforce. <instance> must
# be the exact instance-id the source was tagged with (audit
# correlation; identity assertions key on pid/starttime). The caller
# MUST set QDISTRO_CLIP_SRC_DELAY_MS so the registration lands before
# the source connects and emits (the helper delays inside its own exe,
# keeping the record's exe axis valid).
# read_launch_record <path> <token> — poll for the pid file qdistro-secctx-exec
# publishes via QDISTRO_LAUNCH_RECORD_PATH (it fork()s; the wayland client is
# its child and survives execvp). The file is "<pid> <token>"; the token is
# verified so a same-uid squatter cannot make us register a wrong pid — the
# same contract the spawn's RegisterLaunch path uses.
read_launch_record() {
    local path="$1" tok="$2" i line=""
    for i in $(seq 1 100); do
        line=$(cat "$path" 2>/dev/null) && [ -n "$line" ] && break
        sleep 0.1
    done
    [ -n "$line" ] || return 1
    [ "${line##* }" = "$tok" ] || return 1
    printf '%s\n' "${line%% *}"
}

register_clip_source() {
    local silo="$1" inst="$2" pid="$3" st out
    st=$(starttime "$pid") || return 1
    [ -n "$st" ] || return 1
    out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.RegisterLaunch \
        "string:$silo" "string:qdistro.tier3s" "string:qdistro.tier3s.$silo" \
        "string:$inst" "string:" "uint64:$pid" "string:tier3s" \
        "uint64:$st" 2>&1) || return 1
    printf '%s\n' "$out" | grep -qE 'string "[0-9a-f]{32}"'
}

# broker_check_clip <src> <dst> <src_app_id> <engine> [src_pid src_starttime]
# — a root CheckClipboardTransfer probe. The broker admits a root caller
# whose exe is a known D-Bus CLI client and whose argv names the method (the
# qdshell-gate-probe path) — this is the same trusted-caller path qdshell's
# busctl call uses, but with an explicitly relayed source (pid, starttime)
# so the lineage path under enforcement can be exercised deterministically.
broker_check_clip() {   # -> allow|deny|ERR
    local out
    out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardTransfer \
        "string:$1" "string:$2" array:string:"text/plain" \
        "string:$3" "string:probe-dst" "string:$4" \
        boolean:false "uint32:${5:-0}" "uint64:${6:-0}" 2>&1) || { echo "ERR: $out"; return 1; }
    printf '%s\n' "$out" | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g'
}
# broker_check_clip_mime <src> <dst> <app_id> <engine> <mimes...> — same
# probe with a caller-chosen offer list (strict-MIME coverage).
broker_check_clip_mime() {
    local src="$1" dst="$2" app="$3" eng="$4"; shift 4
    local out
    out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardTransfer \
        "string:$src" "string:$dst" "array:string:$(IFS=,; echo "$*")" \
        "string:$app" "string:probe-dst" "string:$eng" \
        boolean:false uint32:0 uint64:0 2>&1) || { echo "ERR: $out"; return 1; }
    printf '%s\n' "$out" | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g'
}
# broker_check_clip_recv <src> <dst> <mime> <src_app_id> <engine>
# [src_pid src_starttime] — a root CheckClipboardReceive probe (the
# receive-time gate; same trusted-caller path, signature ssssssbut).
broker_check_clip_recv() {   # -> allow|deny|ERR
    local out
    out=$(dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.CheckClipboardReceive \
        "string:$1" "string:$2" "string:$3" \
        "string:$4" "string:probe-dst" "string:$5" \
        boolean:false "uint32:${6:-0}" "uint64:${7:-0}" 2>&1) || { echo "ERR: $out"; return 1; }
    printf '%s\n' "$out" | grep -oE 'string "[^"]*"' | tail -1 | sed 's/string //; s/"//g'
}

# --- the bridge pair --------------------------------------------------------

pid_starttime() { starttime "$1"; }   # alias for readability at call sites

# The wayland-secctx listener the bridge client's waypipe connects to, from
# its own environ (secctx-exec injects WAYLAND_DISPLAY=<basename>).
secctx_listener() {   # secctx_listener <token> -> the listener basename
    local bp disp
    bp=$(rec "$1" bridge_client_pid)
    [ -n "$bp" ] || return 1
    disp=$(tr '\0' '\n' < "/proc/$bp/environ" 2>/dev/null | sed -n 's/^WAYLAND_DISPLAY=//p' | head -1)
    [ -n "$disp" ] && [ -S "$ADMIN_RT/$disp" ] || return 1
    printf '%s\n' "$disp"
}

# snapshot_bridge <token> — append the bridge pair's pid+starttime to
# $WORK/<token>.bridge so assert_bridge_gone can prove their absence later.
snapshot_bridge() {   # snapshot_bridge <token>
    local p st
    : > "$WORK/$1.bridge"
    for p in bridge_client bridge_wrapper; do
        local pid="${p}_pid" skey="${p}_starttime"
        pid=$(rec "$1" "$pid"); skey=$(rec "$1" "$skey")
        [ -n "$pid" ] && printf '%s %s\n' "$pid" "$skey" >> "$WORK/$1.bridge"
    done
    # the secctx listener path (removed when the wrapper's close_fd fires)
    secctx_listener "$1" > "$WORK/$1.listener" 2>/dev/null || :
    # the launch record path (the spawn removes it after RegisterLaunch; the
    # cleanup also rm -f's it — absence is part of teardown)
    rec "$1" launch_record > "$WORK/$1.launchrec" 2>/dev/null || :
}

# assert_bridge_gone <tag> <token> [secs] — the GUI half of assert_launch_gone:
# bridge client + wrapper dead by pid+starttime, link.sock gone (the
# per-launch dir check in assert_launch_gone already covers the dir itself),
# secctx listener socket revoked, launch record file gone.
assert_bridge_gone() {   # assert_bridge_gone <tag> <token> [secs]
    local tag="$1" tok="$2" secs="${3:-60}" left=0 p st lr ls
    bridge_left() {
        local n=0
        while read -r p st; do
            [ -n "$p" ] || continue
            [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && n=$((n + 1))
        done < "$WORK/$tok.bridge" 2>/dev/null
        [ "$n" -eq 0 ]
    }
    wait_for "$secs" bridge_left
    while read -r p st; do
        [ -n "$p" ] || continue
        [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && left=$((left + 1))
    done < "$WORK/$tok.bridge" 2>/dev/null
    if [ ! -s "$WORK/$tok.bridge" ]; then fail "$tag: no bridge snapshot for $tok (cannot prove absence)"
    elif [ "$left" -eq 0 ]; then pass "$tag: bridge client+wrapper gone (pid+starttime)"
    else fail "$tag: $left bridge processes still alive"; fi
    ls=$(cat "$WORK/$tok.listener" 2>/dev/null)
    if [ -z "$ls" ]; then fail "$tag: no secctx listener captured for $tok"
    elif [ ! -S "$ADMIN_RT/$ls" ]; then pass "$tag: secctx listener $ls revoked"
    else fail "$tag: secctx listener $ADMIN_RT/$ls still a socket"; fi
    lr=$(cat "$WORK/$tok.launchrec" 2>/dev/null)
    if [ -z "$lr" ]; then fail "$tag: no launch_record path captured for $tok"
    elif [ ! -e "$lr" ]; then pass "$tag: launch record $lr removed"
    else fail "$tag: launch record $lr still present"; fi
}

# bridge_stream_live <token> — the waypipe client runs with -o (one shot):
# it unlinks $LAUNCHES/<token>/link.sock the moment the sandbox's waypipe
# server attaches, so post-attach there is no pathname to stat. There is
# also no named app-facing socket to probe with a second client: waypipe
# server hands the workload its display over fd-passing (the phaseS spike
# showed /run/user/1000 empty; gVisor names nothing in /proc/net/unix),
# and the -o client cannot be re-dialed. The sandbox-side observable is
# the waypipe server — the container's pid 1 — holding >=2 socket fds:
# the link.sock channel plus the app's wayland connection.
bridge_stream_live() {   # bridge_stream_live <token> -> 0 iff the channel is up
    local tok="$1" ctr
    ctr=$(rec "$tok" container)
    [ -n "$ctr" ] || return 1
    pm exec "$ctr" sh -c '
        [ "$(cat /proc/1/comm 2>/dev/null)" = waypipe ] || exit 1
        n=$(ls -l /proc/1/fd 2>/dev/null | grep -c "socket:")
        [ "${n:-0}" -ge 2 ]'
}

# gofer_pid_of <token> — the runsc gofer process serving this launch's
# container. gVisor's host-uds passthrough moves the host end of the
# accepted link.sock channel into the GOFER's network namespace (the
# waypipe client's accepted fd lives there, peered with the sentry's
# sandbox end), so channel-topology evidence is
# `nsenter -t <gofer> -n ss -xp`, not a host-namespace ss.
gofer_pid_of() {   # -> pid or ""
    local tok="$1" cid
    cid=$(pm inspect --format '{{.Id}}' "$(rec "$tok" container)" 2>/dev/null) || return 1
    [ -n "$cid" ] || return 1
    pgrep -f "runsc-gofer .*${cid}" | head -1
}

# assert_gui_bridge_up <tag> <token> — the live-side counterpart: bridge pair
# alive with matching starttimes, in the launch unit's cgroup; the link.sock
# stream established (the path is unlinked at accept — see bridge_stream_live);
# the secctx listener present.
assert_gui_bridge_up() {   # assert_gui_bridge_up <tag> <token>
    local tag="$1" tok="$2" bp bs wp ws unit
    unit=$(rec "$tok" unit)
    bp=$(rec "$tok" bridge_client_pid); bs=$(rec "$tok" bridge_client_starttime)
    wp=$(rec "$tok" bridge_wrapper_pid); ws=$(rec "$tok" bridge_wrapper_starttime)
    if [ -n "$bp" ] && [ "$(starttime "$bp" 2>/dev/null)" = "$bs" ]; then
        pass "$tag: bridge client pid $bp live (starttime verified)"
    else fail "$tag: bridge client pid '${bp:-?}' dead or starttime drifted"; fi
    if [ -n "$wp" ] && [ "$(starttime "$wp" 2>/dev/null)" = "$ws" ]; then
        pass "$tag: bridge wrapper pid $wp live (starttime verified)"
    else fail "$tag: bridge wrapper pid '${wp:-?}' dead or starttime drifted"; fi
    is "$tag: bridge client runs as admin, exec waypipe" \
        "$(stat -c %u "/proc/$bp" 2>/dev/null):$(cat "/proc/$bp/comm" 2>/dev/null)" "1000:waypipe"
    # the bridge is the launch unit's, not the owning scope's (CONTRACT §5.12)
    is "$tag: bridge client + wrapper in the launch unit cgroup" \
        "$(for p in "$bp" "$wp"; do sed -n 's/^0:://p' "/proc/$p/cgroup" 2>/dev/null; done | grep -c "/${unit}$")" 2
    if bridge_stream_live "$tok"; then
        pass "$tag: bridge channel live (sandbox waypipe server holds channel + app sockets)"
    else
        pm exec "$(rec "$tok" container)" sh -c \
            'cat /proc/1/comm 2>/dev/null; ls -l /proc/1/fd 2>/dev/null; cat /proc/net/unix 2>/dev/null' \
            | sed 's/^/    probe: /' | head -15
        fail "$tag: the sandbox waypipe server is missing its channel or app sockets"
    fi
}

# Bring a GUI silo up live: StartSilo -> record phase=running -> the bridge
# asserts -> the tagged toplevel observed by qdshell. Prints the token.
# up_gui_silo <silo>
up_gui_silo() {
    local s="$1" unit tok cur h
    unit=$(unit_of "$s"); cur=$(journal_cursor)
    sm StartSilo s "$s" > /dev/null || { echo ""; return 1; }
    if ! wait_for 150 bash -c "journalctl -u '$unit' --no-pager -o cat --after-cursor='$cur' | grep -q 'spawn-tier3s: running: '"; then
        unit_log "$unit" "$cur" | tail -20 >&2; echo ""; return 1
    fi
    tok=$(token_of_unit "$unit")
    [ -n "$tok" ] || { echo ""; return 1; }
    snapshot_launch "$tok"; snapshot_bridge "$tok"
    # the compositor + qdshell see the tagged toplevel once the sandboxed app
    # maps through the bridge — wait for qdshell's own observation line so the
    # caller can grep its handle/compositor evidence deterministically.
    if ! wait_for 90 bash -c "journalctl _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat | grep -q '\\[tier3s\\] toplevel observed silo=$s '"; then
        echo "up_gui_silo: $s: no '[tier3s] toplevel observed silo=$s' in the qdshell journal" >&2
        echo ""; return 1
    fi
    echo "$tok"
}

# audit_count <action>: rows in the broker audit DB carrying this exact action.
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
audit_count() { sqlite3 "$AUDIT_DB" "SELECT count(*) FROM audit WHERE action='$1';" 2>/dev/null || echo QUERY-FAILED; }
audit_last_source() {   # audit_last_source <action> -> the newest row's source column
    sqlite3 "$AUDIT_DB" "SELECT source FROM audit WHERE action='$1' ORDER BY id DESC LIMIT 1;" 2>/dev/null
}
