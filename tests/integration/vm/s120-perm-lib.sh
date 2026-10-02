#!/bin/bash
# s120-perm-lib — shared helpers for the permissions-headless drivers
# (s121..s125). SOURCED, never executed. Runs INSIDE the test VM as root.
#
# These drivers replace the broker-only permissions-gui scenarios (the
# labwc-lane .md files whose hard checks were D-Bus replies, sqlite rows,
# audit rows and signals). Every check goes to the REAL broker on the
# system bus (org.qdistro.AdminBroker1) and the real sqlite stores; nothing
# here re-implements broker logic. Fixture SEEDING may use the broker's own
# store modules (ApprovalCache / AuditLog), exactly like the scenarios did.
#
# Conventions:
#   pass "<text>"  -> prints "PASS: <text>"
#   fail "<text>"  -> prints "FAIL: <text>" and marks the run failed; the
#                     driver exits 1 at the end (or immediately via die).
#   Each driver isolates the broker state it touches (rules.d, broker.conf)
#   and restores it on EXIT, so the bats cases are order-independent.

set -u

BUS=org.qdistro.AdminBroker1
OBJ=/org/qdistro/AdminBroker1
RULES_DIR=/etc/qdistro/rules.d
BROKER_CONF=/etc/qdistro/broker.conf
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
APPROVALS_DB=/var/lib/qdistro/approvals/approvals.sqlite
WORK=/tmp/s120
mkdir -p "$WORK"; chmod 1777 "$WORK"

_S120_FAILED=0
pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; _S120_FAILED=1; }
note() { printf 'INFO: %s\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*"; _S120_FAILED=1; exit 1; }
# check <description> <command...> — PASS/FAIL on the command's status.
check() { local d=$1; shift; if "$@"; then pass "$d"; else fail "$d"; fi; }
# expect_eq <description> <got> <want>
expect_eq() {
    if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (got '$2', want '$3')"; fi
}

# Unique per-run tag so rows/actions from one case never collide with another.
TAG="h$(date +%s)$$"

# --- user plumbing -----------------------------------------------------------
# user_of <uid> — login name for a uid (die if absent).
user_of() { getent passwd "$1" | cut -d: -f1; }

# ensure_uid_user <name> <uid> — the broker cases need a caller at a given uid.
# Prefer an existing account; else create a REAL silo through SessionManager1
# (the product path, same as spin-test-vm-gui.sh), falling back to useradd only
# when the session manager is unavailable (the broker itself only sees a uid).
ensure_uid_user() {
    local name=$1 uid=$2 have
    have=$(user_of "$uid")
    if [ -n "$have" ]; then printf '%s\n' "$have"; return 0; fi
    systemctl start qdistro-session-manager.service >/dev/null 2>&1 || true
    runuser -u admin -- busctl --system call org.qdistro.SessionManager1 \
        /org/qdistro/SessionManager1 org.qdistro.SessionManager1 \
        CreateSilo si "$name" "$uid" >"$WORK/createsilo-$name.out" 2>&1 || true
    if [ -z "$(user_of "$uid")" ]; then
        useradd -m -u "$uid" "$name" >/dev/null 2>&1 || true
    fi
    have=$(user_of "$uid")
    [ -n "$have" ] || die "no user with uid $uid and could not create $name"
    printf '%s\n' "$have"
}

# py_as <user> — run python3 (dbus-python) from stdin as <user>.
py_as() { local u=$1; shift; runuser -u "$u" -- python3 - "$@"; }

# broker_call_as <user> <Method> <json-args> — generic dbus-python call.
# Prints "OK <python-repr-of-reply>" or "ERR <dbus error name> <message>".
# json-args is a JSON array; ints stay ints, strings stay strings. Use
# broker_call_typed for methods that need explicit D-Bus types.
broker_call_as() {
    local u=$1 m=$2 args=$3
    runuser -u "$u" -- python3 - "$m" "$args" <<'PY'
import json, sys, dbus
m, args = sys.argv[1], json.loads(sys.argv[2])
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
                                      "/org/qdistro/AdminBroker1"),
                       "org.qdistro.AdminBroker1")
conv = []
for a in args:
    if isinstance(a, dict):
        conv.append(dbus.Dictionary({k: dbus.String(v) for k, v in a.items()},
                                    signature="sv"))
    else:
        conv.append(a)
try:
    r = getattr(iface, m)(*conv, timeout=60)
    def plain(x):
        if isinstance(x, (dbus.Array, list)):
            return [plain(i) for i in x]
        if isinstance(x, (dbus.Dictionary, dict)):
            return {str(k): plain(v) for k, v in x.items()}
        if isinstance(x, dbus.Boolean):
            return bool(x)
        if isinstance(x, (dbus.Int32, dbus.Int64, dbus.UInt32, dbus.UInt64,
                          dbus.Int16, dbus.UInt16, dbus.Byte)):
            return int(x)
        if isinstance(x, (dbus.String, dbus.ObjectPath)):
            return str(x)
        if isinstance(x, tuple) or isinstance(x, dbus.Struct):
            return [plain(i) for i in x]
        return x
    print("OK " + json.dumps(plain(r)))
except dbus.DBusException as e:
    print("ERR %s %s" % (e.get_dbus_name(), e.get_dbus_message()))
PY
}

# bcall_as <user> <Method> <signature> [args...] — busctl call as <user>
# (busctl's argv names the broker + method, which is what the broker's
# admin-control peer check binds non-stdin methods to). Prints busctl's reply
# (e.g. `s "allow"`); returns busctl's status.
bcall_as() {
    local u=$1 m=$2 sig=$3; shift 3
    runuser -u "$u" -- busctl --system call "$BUS" "$OBJ" "$BUS" "$m" "$sig" "$@" 2>&1
}
# dsend_as <user> <Method> [typed dbus-send args...] — dbus-send as <user>;
# on error dbus-send prints "Error <name>: <msg>", which is what the
# error-NAME assertions grep.
dsend_as() {
    local u=$1 m=$2; shift 2
    runuser -u "$u" -- dbus-send --system --print-reply --reply-timeout=20000 \
        --dest="$BUS" "$OBJ" "$BUS.$m" "$@" 2>&1
}

# reply_ok <reply> — strip the "OK " prefix (JSON); empty on error.
reply_json() { case $1 in "OK "*) printf '%s' "${1#OK }" ;; *) printf '' ;; esac; }
reply_err()  { case $1 in "ERR "*) set -- ${1#ERR }; printf '%s' "$1" ;; *) printf '' ;; esac; }

# pending_ids_for <action> — ids of pending requests for an exact action.
pending_ids_for() {
    local r
    r=$(broker_call_as admin GetPending '[]')
    reply_json "$r" | python3 -c '
import json,sys
want=sys.argv[1]
try: rows=json.load(sys.stdin)
except Exception: sys.exit(0)
for row in rows:
    if row.get("action")==want: print(row.get("id"))' "$1"
}
pending_row_json() {  # <action> — the first pending row as JSON
    local r
    r=$(broker_call_as admin GetPending '[]')
    reply_json "$r" | python3 -c '
import json,sys
want=sys.argv[1]
try: rows=json.load(sys.stdin)
except Exception: sys.exit(0)
for row in rows:
    if row.get("action")==want: print(json.dumps(row)); break' "$1"
}
# wait_pending <action> [tries] — poll until a pending row appears (0.25s steps).
wait_pending() {
    local a=$1 n=${2:-120} i
    for ((i = 0; i < n; i++)); do
        [ -n "$(pending_ids_for "$a")" ] && return 0
        sleep 0.25
    done
    return 1
}

sql_audit()     { sqlite3 "$AUDIT_DB" "$1"; }
sql_approvals() { sqlite3 "$APPROVALS_DB" "$1"; }

# --- broker lifecycle --------------------------------------------------------
broker_ready() {
    local i
    for ((i = 0; i < 100; i++)); do
        busctl --system status "$BUS" >/dev/null 2>&1 && \
            [ "$(reply_json "$(broker_call_as admin GetPending '[]')")" != "" ] && return 0
        sleep 0.2
    done
    return 1
}
# Several cases restart the broker back to back (lineage posture flips and
# their EXIT restore); reset systemd's start-rate accounting first so a
# legitimate restart can never trip StartLimitBurst ("start-limit-hit") and
# leave the NEXT case without a broker.
broker_restart() {
    systemctl reset-failed qdistro-admin-broker.service >/dev/null 2>&1 || true
    systemctl restart qdistro-admin-broker.service \
        || die "qdistro-admin-broker.service failed to restart"
    broker_ready || die "broker did not answer GetPending after restart"
}
reload_rules() { broker_call_as admin ReloadRules '[]'; }

# Every driver starts from a live broker (a previous case's failure must not
# cascade into this one as NameHasNoOwner).
if ! systemctl is-active --quiet qdistro-admin-broker.service; then
    systemctl reset-failed qdistro-admin-broker.service >/dev/null 2>&1 || true
    systemctl start qdistro-admin-broker.service >/dev/null 2>&1 || true
fi
broker_ready || { echo "FAIL: broker not answering at driver start"; exit 1; }

# --- rules.d isolation -------------------------------------------------------
# isolate_rules — move every existing rule file aside for the duration of the
# driver (cases assert "no rule matches" / exact rule counts) and restore them
# on EXIT. Also restores broker.conf if a case touched it.
_S120_RULES_BAK=""
_S120_CONF_BAK=""
_S120_CONF_TOUCHED=0
_s120_restore() {
    local rc=$?
    if [ -n "$_S120_RULES_BAK" ] && [ -d "$_S120_RULES_BAK" ]; then
        rm -f "$RULES_DIR"/*.yaml "$RULES_DIR"/*.yml 2>/dev/null
        cp -a "$_S120_RULES_BAK"/. "$RULES_DIR"/ 2>/dev/null
        rm -rf "$_S120_RULES_BAK"
    fi
    if [ "$_S120_CONF_TOUCHED" = 1 ]; then
        if [ -n "$_S120_CONF_BAK" ] && [ -f "$_S120_CONF_BAK" ]; then
            cp -a "$_S120_CONF_BAK" "$BROKER_CONF"; rm -f "$_S120_CONF_BAK"
        else
            rm -f "$BROKER_CONF"
        fi
        systemctl reset-failed qdistro-admin-broker.service >/dev/null 2>&1 || true
        systemctl restart qdistro-admin-broker.service >/dev/null 2>&1 || true
        broker_ready >/dev/null 2>&1 || true
    fi
    reload_rules >/dev/null 2>&1 || true
    [ "$_S120_FAILED" = 0 ] || rc=1
    exit "$rc"
}
trap _s120_restore EXIT

isolate_rules() {
    install -d -m 0755 "$RULES_DIR"
    _S120_RULES_BAK=$(mktemp -d /var/tmp/s120-rules-bak.XXXXXX)
    cp -a "$RULES_DIR"/. "$_S120_RULES_BAK"/ 2>/dev/null
    rm -f "$RULES_DIR"/*.yaml "$RULES_DIR"/*.yml 2>/dev/null
    local r
    r=$(reload_rules)
    case $r in
        "OK [0, []]") pass "rules baseline isolated: ReloadRules counted 0 rules, no errors" ;;
        *) fail "rules baseline not empty after isolation: $r" ;;
    esac
}

# set_lineage_enforce <true|false|unset> — edit broker.conf (restored on EXIT)
# and restart; returns once the broker logs the posture line AFTER a cursor.
set_lineage_enforce() {
    local want=$1 cur
    if [ "$_S120_CONF_TOUCHED" = 0 ]; then
        _S120_CONF_TOUCHED=1
        if [ -f "$BROKER_CONF" ]; then
            _S120_CONF_BAK=$(mktemp /var/tmp/s120-broker-conf.XXXXXX)
            cp -a "$BROKER_CONF" "$_S120_CONF_BAK"
        fi
    fi
    install -d -m 0755 /etc/qdistro
    touch "$BROKER_CONF"
    sed -i '/^[[:space:]]*lineage_enforce/d' "$BROKER_CONF"
    [ "$want" = unset ] || echo "lineage_enforce = $want" >> "$BROKER_CONF"
    cur=$(journalctl -u qdistro-admin-broker.service -n0 --show-cursor 2>/dev/null \
          | sed -n 's/^-- cursor: //p')
    broker_restart
    local pat="lineage_enforce=False"
    [ "$want" = true ] && pat="lineage_enforce=True"
    local i
    for ((i = 0; i < 60; i++)); do
        if [ -n "$cur" ] && journalctl -u qdistro-admin-broker.service \
                --after-cursor "$cur" --no-pager -o cat 2>/dev/null | grep -q "$pat"; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

# save_rule <filename> <yaml> — SaveRule as admin; prints the reply line.
save_rule() {
    runuser -u admin -- python3 - "$1" "$2" <<'PY'
import sys, dbus
iface = dbus.Interface(dbus.SystemBus().get_object(
    "org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1"),
    "org.qdistro.AdminBroker1")
try:
    print("OK " + str(iface.SaveRule(sys.argv[1], sys.argv[2])))
except dbus.DBusException as e:
    print("ERR %s %s" % (e.get_dbus_name(), e.get_dbus_message()))
PY
}

# start_signal_monitor <member> <logfile> — dbus-monitor on a broker signal,
# with a positive-control readiness probe (a private QciProbe signal the
# monitor must see) so "no signal in the log" can never mean "not subscribed".
# Records the monitor's own pid in <logfile>.pid.
start_signal_monitor() {
    local member=$1 log=$2 tok p i
    rm -f "$log" "$log.pid"
    setsid -f /bin/sh -c 'echo $$ >"$1"; exec dbus-monitor --system "$2" "$3" >"$4" 2>&1 </dev/null' \
        _ "$log.pid" \
        "type=signal,interface=org.qdistro.AdminBroker1,member=$member" \
        "type=signal,interface=org.qdistro.QciProbe,member=Ready" "$log"
    tok="ready-$$-$RANDOM"
    for ((i = 0; i < 100; i++)); do
        dbus-send --system --type=signal /org/qdistro/QciProbe \
            org.qdistro.QciProbe.Ready "string:$tok" 2>/dev/null
        grep -q "$tok" "$log" 2>/dev/null && return 0
        sleep 0.1
    done
    return 1
}
stop_signal_monitor() {  # <logfile> — kill by recorded pid + comm identity
    local p
    p=$(cat "$1.pid" 2>/dev/null)
    case $p in ''|*[!0-9]*) return 0 ;; esac
    [ "$(cat "/proc/$p/comm" 2>/dev/null)" = dbus-monitor ] && kill "$p" 2>/dev/null
    sleep 0.3
    return 0
}

# install_test_permission — the SDK acceptance client (tests/unit/
# test_permission.py, installed as qdistro-test-permission on the GUI lane)
# made runnable by any uid from $WORK, against the in-VM source tree.
TP="$WORK/qdistro-test-permission"
install_test_permission() {
    local src=/root/qdistro-src
    if [ -x /usr/local/bin/qdistro-test-permission ]; then
        TP=/usr/local/bin/qdistro-test-permission; return 0
    fi
    install -m 0755 "$src/tests/unit/test_permission.py" "$TP" \
        || die "cannot stage test_permission.py from $src"
    if ! python3 -c 'import qdistro_app' 2>/dev/null; then
        rm -rf "$WORK/sdk"; install -d -m 0755 "$WORK/sdk"
        cp -a "$src/sdk/qdistro_app" "$WORK/sdk/" || die "cannot stage the SDK"
        chmod -R a+rX "$WORK/sdk"
    fi
}
# run_test_permission <user> <action> — prints ALLOWED/DENIED; returns its rc.
run_test_permission() {
    local u=$1 a=$2
    timeout 120 runuser -u "$u" -- env PYTHONPATH="$WORK/sdk" \
        python3 "$TP" --action "$a" --detail "purpose=s120-$a"
}
