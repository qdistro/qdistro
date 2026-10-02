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
# Type=notify), so the call gets more than busctl's default 25 s
sm() {
    as_admin busctl --system --timeout=150 call org.qdistro.SessionManager1 /org/qdistro/SessionManager1 \
        org.qdistro.SessionManager1 "$@"
}
broker_check() {   # broker_check <action> -> allow|deny|unknown|ERR
    local out
    out="$(as_admin busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' "$1" 0 2>&1)" || { echo ERR; return; }
    out="${out#s \"}"; echo "${out%\"}"
}
silo_state() {   # silo_state <name> -> its ListSilos state, or "absent"
    sm ListSilos | sed 's/^s "//; s/"$//; s/\\"/"/g' | python3 -c '
import json, sys
for s in json.load(sys.stdin):
    if s["name"] == sys.argv[1]: print(s["state"]); break
else: print("absent")' "$1"
}
wait_for() {   # wait_for <secs> <cmd...>
    local n="$1"; shift
    for _ in $(seq 1 $((n * 4))); do "$@" && return 0; sleep 0.25; done
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
    local tok
    tok=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    install -d -m 0755 /run/qdistro/silo-launch
    ( umask 077
      printf '%s\n' "TIER3S_SILO=$1" "TIER3S_BINDING=$1" "TIER3S_WORKLOAD=headless-smoke" "TIER3S_NETWORK=none" \
          "TIER3S_LAUNCH_TOKEN=$tok" "TIER3S_ARGV_JSON='$2'" > "/run/qdistro/silo-launch/$1.env" )
    chmod 0600 "/run/qdistro/silo-launch/$1.env"
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
}
