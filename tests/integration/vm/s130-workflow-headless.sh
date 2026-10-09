#!/bin/bash
# In-VM driver for workflow-headless.bats — headless equivalents of
# tests/integration/workflow-gui/01, 02, 03 and 05.
#
# Usage: s130-workflow-headless.sh <wf01|wf02|wf03|wf05>
#
# The workflow engine runs INSIDE the admin broker (_setup_workflow_engine).
# Every load-bearing check of the GUI scenarios is a workflow_audit.sqlite
# row, a broker D-Bus reply (ListWorkflows / ListWorkflowRuns /
# PreviewWorkflowRun / ApproveWorkflowRun) or the per-run ssh-agent socket
# under /run/qdistro/workflow-secrets; the admin app's Workflows tab only
# renders ListWorkflows/ListWorkflowRuns. Here we assert those directly.
#
# Deviation from the GUI files (which never ran in CI): workflows without
# `auto_run: true` PARK a pending run (F3, workflow_engine._on_trigger) — so
# the wf01/02/03 YAML as written in workflow-gui/ would never execute. The
# seeds below add `auto_run: true` to 01/02/03; 05 keeps it off on purpose.
#
# PASS strings MUST match assert_output_contains in workflow-headless.bats.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }

SECTION=${1:?usage: $0 <wf01|wf02|wf03|wf05>}
WF_DIR=/etc/qdistro/workflows
WF_DB=/var/lib/qdistro/audit/workflow_audit.sqlite
SECRETS_ROOT=/run/qdistro/workflow-secrets
VAULT=wfhl
VAULT_PW=wfhl-vault-pass-260
BUS=org.qdistro.AdminBroker1
OBJ=/org/qdistro/AdminBroker1
TARGET_PID=""
CG=""
# Runs from an earlier invocation stay in workflow_audit.sqlite (and a broker
# reload leaves their rows in whatever state they had); only look at runs
# started by THIS invocation.
T0=$(( $(date +%s) - 1 ))

finish() {
    if [ "$FAILCOUNT" -eq 0 ]; then
        echo "[s130 $SECTION] $PASSCOUNT passes, 0 failures"; exit 0
    fi
    echo "[s130 $SECTION] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1
}

cleanup() {
    [ -n "$TARGET_PID" ] && kill "$TARGET_PID" 2>/dev/null
    if [ -n "$CG" ] && [ -d "$CG" ]; then
        # Move anything left back to the root cgroup so rmdir can succeed.
        while read -r p; do echo "$p" >/sys/fs/cgroup/cgroup.procs 2>/dev/null; done <"$CG/cgroup.procs"
        rmdir "$CG" 2>/dev/null
    fi
    rm -f "$WF_DIR"/wfhl-*.yaml
    runuser -u admin -- qdistro-pwd-admin delete "$VAULT" sign-key >/dev/null 2>&1 || true
    # The broker reloads workflow definitions on SIGHUP. Avoid one restart
    # per section: four rapid resets hit systemd's service start limit.
    systemctl kill --kill-whom=main --signal=HUP qdistro-admin-broker.service 2>/dev/null || true
}
trap cleanup EXIT

sql() {
    python3 - "$WF_DB" "$1" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=10)
for row in con.execute(sys.argv[2]):
    print("|".join("" if v is None else str(v) for v in row))
PYEOF
}

broker_up() {
    local i
    for i in $(seq 1 60); do
        dbus-send --system --print-reply --dest=$BUS $OBJ \
            org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && return 0
        sleep 0.25
    done
    return 1
}

# admin_call <python-expr-using-iface> — run as admin, print the result as JSON.
admin_json() {
    runuser -u admin -- python3 -c '
import dbus, json, sys
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
def plain(v):
    if isinstance(v, (dbus.Dictionary, dict)): return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (dbus.Array, list, tuple)): return [plain(x) for x in v]
    if isinstance(v, dbus.Boolean): return bool(v)
    if isinstance(v, (dbus.Int32, dbus.UInt32, dbus.Int64, dbus.UInt64)): return int(v)
    if isinstance(v, dbus.Double): return float(v)
    return str(v)
print(json.dumps(plain(eval(sys.argv[1]))))
' "$1"
}

# runs_of <workflow> — "run_id|state|error|digest" lines from ListWorkflowRuns.
runs_of() {
    admin_json 'iface.ListWorkflowRuns(200)' | python3 -c '
import json, sys
for r in json.load(sys.stdin):
    if r["workflow_name"] == sys.argv[1] and r["started_at"] >= float(sys.argv[2]):
        print("|".join([r["run_id"], r["state"], r["error"].replace("|", "/"), r["definition_digest"]]))
' "$1" "$T0"
}

seed() {  # <file> ; YAML on stdin, then reload the broker so it is loaded
    mkdir -p "$WF_DIR"
    rm -f "$WF_DIR"/wfhl-*.yaml
    cat >"$WF_DIR/$1"
    broker_up || { fail "broker unavailable before seeding $1"; finish; }
    systemctl kill --kill-whom=main --signal=HUP qdistro-admin-broker.service \
        || { fail "broker workflow reload signal failed for $1"; finish; }
    local name=${1%.yaml} loaded=""
    for _ in $(seq 1 60); do
        loaded=$(admin_json 'iface.ListWorkflows()' 2>/dev/null | python3 -c '
import json, sys
try:
    names = {w["name"] for w in json.load(sys.stdin)}
except (ValueError, KeyError, TypeError):
    names = set()
print("ready" if sys.argv[1] in names else ",".join(sorted(names)))
' "$name" 2>/dev/null)
        [ "$loaded" = ready ] && return 0
        sleep 0.25
    done
    fail "broker did not load workflow $name after SIGHUP (last ListWorkflows=[$loaded])"
    finish
}

ensure_user() {  # non-admin caller for the AccessDenied leg
    getent passwd work >/dev/null 2>&1 || useradd -m -U -s /bin/bash work >/dev/null 2>&1
}

provision_key() {  # vault $VAULT, tag sign-key, holds a fresh ed25519 key
    local d
    d=$(mktemp -d /run/s130-key.XXXXXX)
    ssh-keygen -t ed25519 -N '' -q -C s130-wfhl -f "$d/key" || { fail "ssh-keygen failed"; finish; }
    KEY_FP=$(ssh-keygen -lf "$d/key.pub" | awk '{print $2}')
    # An empty KEY_FP would make every *"$KEY_FP"* match below vacuous.
    case "$KEY_FP" in SHA256:*) ;; *) fail "no fingerprint from ssh-keygen: [$KEY_FP]"; finish ;; esac
    KEY_MID=$(sed -n 3p "$d/key")            # a body line of the private key
    runuser -u admin -- qdistro-pwd-admin lock "$VAULT" >/dev/null 2>&1 || true
    rm -f "/var/lib/qdistro/vaults/$VAULT.vault"
    runuser -u admin -- bash -c "qdistro-pwd-admin create '$VAULT' < <(printf '%s\n%s\n' '$VAULT_PW' '$VAULT_PW')" \
        >/tmp/s130-create.log 2>&1 || { cat /tmp/s130-create.log; fail "vault create failed"; finish; }
    runuser -u admin -- env QDISTRO_PWD_PASSWORD="$VAULT_PW" qdistro-pwd-admin unlock "$VAULT" \
        2>&1 | grep -qx unlocked || { fail "vault unlock failed"; finish; }
    runuser -u admin -- qdistro-pwd-admin add "$VAULT" sign-key <"$d/key" \
        || { fail "vault add failed"; finish; }
    shred -u "$d/key" 2>/dev/null; rm -rf "$d"
    pass "vault $VAULT unlocked with a fresh ed25519 key ($KEY_FP)"
}

# fire_spawn <cgroup-dir> <seconds> — start `sleep N` and move it into the
# watched cgroup AFTER the poller baselined (it then fires as a NEW spawn).
fire_spawn() {
    CG=$1
    mkdir -p "$CG"
    sleep "$2" </dev/null >/dev/null 2>&1 &
    TARGET_PID=$!
    echo "$TARGET_PID" >"$CG/cgroup.procs"
}

agent_socks() { find "$SECRETS_ROOT" -name agent.sock 2>/dev/null; }

agent_procs_for() {  # count live processes whose cmdline names <path>
    python3 - "$1" <<'PYEOF'
import os, sys
n = 0
for p in os.listdir("/proc"):
    if not p.isdigit() or int(p) == os.getpid(): continue
    try: cmd = open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")
    except OSError: continue
    if cmd and os.path.basename(cmd[0]) == b"ssh-agent" and sys.argv[1].encode() in cmd: n += 1
print(n)
PYEOF
}

wait_state() {  # <workflow> <state> <timeout_s> — prints the matching run line
    local i line
    for i in $(seq 1 $(( $3 * 4 ))); do
        line=$(runs_of "$1" | awk -F'|' -v s="$2" '$2 == s {print; exit}')
        [ -n "$line" ] && { printf '%s\n' "$line"; return 0; }
        sleep 0.25
    done
    return 1
}

broker_up || { fail "broker not reachable"; finish; }

case "$SECTION" in
# ---------------------------------------------------------------------------
wf01)   # one trigger -> one run -> audit row chain
seed wfhl-tick.yaml <<'YAML'
- name: wfhl-tick
  description: cron tick that lists rules (no secret needed)
  auto_run: true
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
YAML
ok=""
for _ in $(seq 1 120); do
    [ -n "$(sql "SELECT 1 FROM workflow_runs WHERE workflow_name='wfhl-tick' AND state='completed' AND started_at >= $T0 LIMIT 1;")" ] && { ok=1; break; }
    sleep 0.25
done
[ -n "$ok" ] && pass "wf01: cron trigger fired a run recorded completed in workflow_runs" \
    || fail "wf01: no completed wfhl-tick run within 30 s: $(sql "SELECT state, error FROM workflow_runs WHERE workflow_name='wfhl-tick';")"
STEP=$(sql "SELECT s.step_type, s.success FROM workflow_steps s JOIN workflow_runs r ON s.run_id=r.run_id WHERE r.workflow_name='wfhl-tick' AND r.state='completed' AND r.started_at >= $T0 ORDER BY s.started_at DESC LIMIT 1;")
[ "$STEP" = "call_broker|1" ] && pass "wf01: the run's step row is call_broker|1" \
    || fail "wf01: step row expected call_broker|1, got [$STEP]"
DEF=$(admin_json 'iface.ListWorkflows()' | python3 -c '
import json, sys
for w in json.load(sys.stdin):
    if w["name"] == "wfhl-tick": print(w["trigger_type"], w["step_count"])')
[ "$DEF" = "cron 1" ] && pass "wf01: ListWorkflows lists wfhl-tick trigger=cron steps=1" \
    || fail "wf01: ListWorkflows wfhl-tick got [$DEF]"
RUN=$(admin_json 'iface.ListWorkflowRuns(200)' | python3 -c '
import json, sys
for r in json.load(sys.stdin):
    if r["workflow_name"] == "wfhl-tick" and r["state"] == "completed" and r["started_at"] >= float(sys.argv[1]):
        print("completed-with-start"); break' "$T0")
[ "$RUN" = completed-with-start ] && pass "wf01: ListWorkflowRuns shows a completed wfhl-tick run with a start time" \
    || fail "wf01: ListWorkflowRuns has no completed wfhl-tick run"
;;
# ---------------------------------------------------------------------------
wf05)   # human-in-the-loop: pending queue gates the run (S1/S2/S4)
ensure_user
seed wfhl-approval.yaml <<'YAML'
- name: wfhl-approval
  description: human-in-the-loop demo (no auto_run)
  trigger:
    type: cron
    interval_seconds: 5
  steps:
    - type: call_broker
      method: ListRules
YAML
LINE=$(wait_state wfhl-approval pending 20) \
    || { fail "wf05: no pending wfhl-approval run within 20 s"; finish; }
RUN_ID=${LINE%%|*}
sleep 11   # >= 2 more cron fires while unapproved
STATES=$(runs_of wfhl-approval | cut -d'|' -f2 | sort | uniq -c | awk '{print $2"="$1}' | paste -sd, -)
[ "$STATES" = "pending=1" ] \
    && pass "wf05: cron fires parked exactly ONE pending run (deduped), none executed" \
    || fail "wf05: run states after 3+ fires expected pending=1, got [$STATES]"
DIGEST=$(runuser -u admin -- gdbus call --system --dest $BUS --object-path $OBJ \
    --method $BUS.PreviewWorkflowRun "$RUN_ID" 2>&1 | python3 -c '
import ast, json, sys
print(json.loads(ast.literal_eval(sys.stdin.read().strip())[0]).get("definition_digest", ""))' 2>&1)
case "$DIGEST" in
    [0-9a-f]*) [ "${#DIGEST}" = 64 ] && pass "wf05: admin PreviewWorkflowRun returned the plan digest" \
                   || fail "wf05: preview digest malformed [$DIGEST]" ;;
    *) fail "wf05: PreviewWorkflowRun failed [$DIGEST]" ;;
esac
NONADMIN=$(runuser -u work -- gdbus call --system --dest $BUS --object-path $OBJ \
    --method $BUS.ApproveWorkflowRun "$RUN_ID" "$DIGEST" 2>&1)
case "$NONADMIN" in
    *AccessDenied*|*"not allowed"*|*"restricted to"*)
        pass "wf05: non-admin ApproveWorkflowRun (even with the right digest) denied" ;;
    *) fail "wf05: non-admin approve was NOT denied: [$NONADMIN]" ;;
esac
WRONG=$(runuser -u admin -- gdbus call --system --dest $BUS --object-path $OBJ \
    --method $BUS.ApproveWorkflowRun "$RUN_ID" "$(printf '0%.0s' $(seq 1 64))" 2>&1)
[ "$WRONG" = "(false,)" ] && pass "wf05: admin approve with a wrong digest returns false" \
    || fail "wf05: wrong-digest approve returned [$WRONG]"
STILL=$(runs_of wfhl-approval | awk -F'|' -v id="$RUN_ID" '$1 == id {print $2}')
[ "$STILL" = pending ] && pass "wf05: run $RUN_ID still pending after the refused approvals" \
    || fail "wf05: run state after refused approvals [$STILL]"
OK=$(runuser -u admin -- gdbus call --system --dest $BUS --object-path $OBJ \
    --method $BUS.ApproveWorkflowRun "$RUN_ID" "$DIGEST" 2>&1)
[ "$OK" = "(true,)" ] && pass "wf05: admin ApproveWorkflowRun(run, previewed digest) accepted" \
    || fail "wf05: admin approve returned [$OK]"
DONE=""
for _ in $(seq 1 80); do
    DONE=$(runs_of wfhl-approval | awk -F'|' -v id="$RUN_ID" '$1 == id {print $2}')
    [ "$DONE" = completed ] && break
    sleep 0.25
done
[ "$DONE" = completed ] && pass "wf05: the SAME run id $RUN_ID executed to completed" \
    || fail "wf05: approved run state [$DONE]"
STEPROW=$(sql "SELECT step_type, success FROM workflow_steps WHERE run_id='$RUN_ID' LIMIT 1;")
[ "$STEPROW" = 'call_broker|1' ] && pass "wf05: the approved run wrote its step row" \
    || fail "wf05: no successful call_broker step for approved run $RUN_ID [$STEPROW]"
NCOMP=$(runs_of wfhl-approval | awk -F'|' '$2 == "completed"' | wc -l)
[ "$NCOMP" = 1 ] && pass "wf05: exactly one completed run (no auto-run)" \
    || fail "wf05: $NCOMP completed runs"
;;
# ---------------------------------------------------------------------------
wf02)   # secret delivery: ephemeral ssh-agent socket exists, then gone
command -v ssh-agent >/dev/null && command -v ssh-add >/dev/null \
    || { fail "wf02: ssh-agent/ssh-add not installed"; finish; }
provision_key
JSTART=$(date '+%Y-%m-%d %H:%M:%S')
seed wfhl-git-sign.yaml <<'YAML'
- name: wfhl-git-sign
  description: deliver SSH signing key for one process, then scrub
  auto_run: true
  trigger:
    type: process_spawn
    cgroup_pattern: "system.slice/qdistro-wfhl-sign-*.scope"
    poll_interval: 0.25
  needs:
    - vault/wfhl/sign-key
  steps:
    - deliver_secret:
        item: vault/wfhl/sign-key
        as: ssh-agent
        expose_as: SSH_AUTH_SOCK
        ttl: 120
        scrub_on: workflow_exit
    - wait_for_process: $trigger.pid
YAML
sleep 2   # the process_spawn poller baselines the (absent) cgroup
fire_spawn /sys/fs/cgroup/system.slice/qdistro-wfhl-sign-test.scope 60
SOCK=""
for _ in $(seq 1 60); do SOCK=$(agent_socks | head -1); [ -n "$SOCK" ] && break; sleep 0.25; done
case "$SOCK" in
    "$SECRETS_ROOT"/ssh-*/agent.sock) pass "wf02: per-run agent socket exists while the process lives ($SOCK)" ;;
    *) fail "wf02: no agent.sock under $SECRETS_ROOT: [$SOCK] runs=[$(runs_of wfhl-git-sign)]"; finish ;;
esac
# The agent binds its socket before deliver() loads the key, so sock
# existence is not key readiness — poll the CONTENT until the fingerprint
# shows up (bounded; a missing key after the deadline is a real failure,
# same assertion as before).
FP=""
for _ in $(seq 1 40); do
    FP=$(SSH_AUTH_SOCK="$SOCK" ssh-add -l 2>&1)
    case "$FP" in *"$KEY_FP"*) break ;; esac
    sleep 0.25
done
case "$FP" in
    *"$KEY_FP"*) pass "wf02: the agent holds the vault key (fingerprint matches)" ;;
    *) fail "wf02: agent key list [$FP] lacks $KEY_FP" ;;
esac
AGENT_N=$(agent_procs_for "$SOCK")
[ "$AGENT_N" = 1 ] || fail "wf02: expected 1 ssh-agent for $SOCK, found $AGENT_N"
RUNNING=$(runs_of wfhl-git-sign | awk -F'|' '$2 == "running"' | wc -l)
[ "$RUNNING" = 1 ] && pass "wf02: ListWorkflowRuns shows the run running while wait_for_process blocks" \
    || fail "wf02: running runs = $RUNNING [$(runs_of wfhl-git-sign)]"
DEF=$(admin_json 'iface.ListWorkflows()' | python3 -c '
import json, sys
for w in json.load(sys.stdin):
    if w["name"] == "wfhl-git-sign": print(w["trigger_type"], w["step_count"], ",".join(w["needs"]))')
[ "$DEF" = "process_spawn 2 vault/wfhl/sign-key" ] && pass "wf02: ListWorkflows lists trigger=process_spawn steps=2 needs=vault/wfhl/sign-key" \
    || fail "wf02: ListWorkflows got [$DEF]"
kill "$TARGET_PID" 2>/dev/null; wait "$TARGET_PID" 2>/dev/null; TARGET_PID=""
GONE=""
for _ in $(seq 1 60); do
    if [ -z "$(agent_socks)" ] && [ -z "$(ls -A "$SECRETS_ROOT" 2>/dev/null)" ]; then GONE=1; break; fi
    sleep 0.25
done
[ -n "$GONE" ] && pass "wf02: after the process exits the agent socket and its ssh-* dir are gone" \
    || fail "wf02: secrets left behind: $(agent_socks) $(ls -A "$SECRETS_ROOT" 2>/dev/null)"
AGENT_N=$(agent_procs_for "$SOCK")
[ "$AGENT_N" = 0 ] && pass "wf02: the per-run ssh-agent process is gone" \
    || fail "wf02: $AGENT_N ssh-agent still running for $SOCK"
wait_state wfhl-git-sign completed 10 >/dev/null \
    && pass "wf02: the run is completed" \
    || fail "wf02: run did not complete [$(runs_of wfhl-git-sign)]"
LEAK=$(python3 - "$WF_DB" "$KEY_MID" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dump = "\n".join(con.iterdump())
print(int("PRIVATE KEY" in dump) + int(sys.argv[2] in dump))
PYEOF
)
JLEAK=$(journalctl -u qdistro-admin-broker.service --since "$JSTART" --no-pager 2>/dev/null | grep -cF -e 'PRIVATE KEY' -e "$KEY_MID")
[ "$LEAK" = 0 ] && [ "${JLEAK:-0}" = 0 ] \
    && pass "wf02: no private-key material in the workflow audit DB or the broker journal" \
    || fail "wf02: key material leaked (db=$LEAK journal=$JLEAK)"
;;
# ---------------------------------------------------------------------------
wf03)   # failure mid-step -> secrets scrubbed -> run marked failed
command -v ssh-agent >/dev/null || { fail "wf03: ssh-agent not installed"; finish; }
provision_key
seed wfhl-failstep.yaml <<'YAML'
- name: wfhl-failstep
  description: deliver a secret then fail on a forbidden call_broker
  auto_run: true
  trigger:
    type: process_spawn
    cgroup_pattern: "system.slice/qdistro-wfhl-fail-*.scope"
    poll_interval: 0.25
  needs:
    - vault/wfhl/sign-key
  steps:
    - deliver_secret:
        item: vault/wfhl/sign-key
        as: ssh-agent
        scrub_on: workflow_exit
    - type: call_broker
      method: DecideRequest
YAML
sleep 2
fire_spawn /sys/fs/cgroup/system.slice/qdistro-wfhl-fail-test.scope 30
LINE=$(wait_state wfhl-failstep failed 15) \
    && pass "wf03: the run is recorded failed" \
    || fail "wf03: no failed wfhl-failstep run [$(runs_of wfhl-failstep)]"
STEPS=$(sql "SELECT s.step_type, s.success FROM workflow_steps s JOIN workflow_runs r ON s.run_id=r.run_id WHERE r.workflow_name='wfhl-failstep' AND r.started_at >= $T0 ORDER BY s.started_at;" | paste -sd, -)
[ "$STEPS" = "deliver_secret|1,call_broker|0" ] \
    && pass "wf03: steps deliver_secret|1 then call_broker|0 (delivery worked, forbidden call failed)" \
    || fail "wf03: step rows expected deliver_secret|1,call_broker|0, got [$STEPS]"
GONE=""
for _ in $(seq 1 40); do
    if [ -z "$(agent_socks)" ] && [ -z "$(ls -A "$SECRETS_ROOT" 2>/dev/null)" ]; then GONE=1; break; fi
    sleep 0.25
done
[ -n "$GONE" ] && pass "wf03: the delivered secret was scrubbed despite the failure (no agent.sock, no ssh-* dir)" \
    || fail "wf03: secrets left behind after the failed run: $(agent_socks) $(ls -A "$SECRETS_ROOT" 2>/dev/null)"
ERR=$(printf '%s\n' "$LINE" | cut -d'|' -f3)
case "$ERR" in
    *"not in whitelist"*) pass "wf03: ListWorkflowRuns error column carries the call_broker whitelist refusal" ;;
    *) fail "wf03: ListWorkflowRuns error column [$ERR]" ;;
esac
;;
*) fail "unknown section $SECTION" ;;
esac

finish
