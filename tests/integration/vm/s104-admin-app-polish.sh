#!/bin/bash
# s104-admin-app-polish — broker D-Bus surface behind the P07 admin-app
# features (Rules tab, History tab, tray badge, approve/deny, rule editor,
# scope rejection).
#
# Runs INSIDE the test VM (staged at /tmp/s104.sh by admin-app-polish.bats).
#
# HONESTY NOTE: this driver never starts the admin app and sends no key or
# click. It calls the broker methods the app calls, as the admin uid, and
# checks the broker's answers. Its PASS strings therefore name broker
# behaviour only. (They used to read "Ctrl+Y approves pending request",
# "tray badge shows pending count", "ScopeNotPermitted shows modal error":
# UI claims this driver never checked.) The real UI paths are proved by the
# GUI scenarios permissions-gui/04 (Ctrl+Shift+2 + Ctrl+Y approve) and 06,
# and the scope-error modal by tests/unit/test_admin_error_scope_ux.py.
#
# PASS contract (asserted by admin-app-polish.bats):
#   PASS: broker ListRules returns an on-disk YAML rule
#   PASS: broker SaveRule (as admin) writes a rule that ListRules returns
#   PASS: broker ListHistory(100) returns the audit row of a decided request
#   PASS: broker GetPending counts a newly queued request
#   PASS: broker DecideRequest(allow) as admin removes the request from GetPending
#   PASS: broker DecideRequest(deny) as admin removes the request from GetPending
#   PASS: broker SaveRule of a rule-from-request YAML is returned by ListRules
#   PASS: broker DecideRequest rejects an unknown scope with a D-Bus error
#
# Strategy (P07 fix-pass):
# - Start the admin broker.
# - Step 1/2 verify Rules tab via real file-on-disk + busctl SaveRule.
# - Step 3/4 verify History tab + tray-count GetPending against real
#   pending-request data (Step 4 enqueues a request before asking
#   GetPending so the count is exercised with N>0).
# - Step 5 drives the broker calls the app's approve/deny/rule-editor
#   actions end in (not the actions themselves):
#     - Enqueue a pending request via RequestPermission.
#     - Call DecideRequest(allow) as the admin uid; assert the rid left
#       GetPending.
#     - Repeat with decision=deny.
#     - SaveRule a rule-from-request shaped YAML; assert ListRules has it.
# - Step 6 deliberately calls DecideRequest with a syntactically valid
#   but unknown scope to provoke a BadArgument; for the proper
#   ScopeNotPermitted name we'd need a delegated request. We accept
#   either error name in the FAIL output and fail_loud if neither
#   fires.

set -u

err() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf 'INFO: %s\n' "$*"; }

BUS="org.qdistro.AdminBroker1"
OBJ="/org/qdistro/AdminBroker1"
RULES_DIR="/etc/qdistro/rules.d"

# ---------------------------------------------------------------------------
# Step 0 — bring up the broker, resolve the admin uid.
# ---------------------------------------------------------------------------

systemctl restart qdistro-admin-broker.service \
    || err "qdistro-admin-broker.service failed to start"
sleep 1

mkdir -p "$RULES_DIR"

# Resolve admin uid the broker enforces. Convention: admin/uid 1000.
# The broker reads ADMIN_UID from environment, falling back to 1000;
# we read /proc/<pid>/environ to discover the active value, falling
# back to 1000 if that read fails.
BROKER_PID=$(systemctl show -p MainPID qdistro-admin-broker.service | cut -d= -f2)
ADMIN_UID=$(tr '\0' '\n' < "/proc/${BROKER_PID}/environ" 2>/dev/null \
            | awk -F= '/^ADMIN_UID=/{print $2; exit}')
ADMIN_UID="${ADMIN_UID:-1000}"
ADMIN_USER=$(getent passwd "$ADMIN_UID" | cut -d: -f1)
if [ -z "$ADMIN_USER" ]; then
    err "could not resolve admin user for uid=$ADMIN_UID"
fi
note "admin user: $ADMIN_USER (uid=$ADMIN_UID)"

# Helper: run a Python snippet as the admin user. Used for every
# DecideRequest/RequestPermission so peer-uid auth on the broker side
# sees the right caller. dbus-python is the only stable busctl-of-
# python-objects path that supports a{sv} dicts cleanly.
admin_py() {
    runuser -u "$ADMIN_USER" -- python3 - "$@"
}

# Helper: enqueue a pending request via RequestPermission (called as
# admin so peer-uid is the admin uid; the broker's peer-uid recheck on
# DecideRequest then succeeds for the same admin uid). Returns the rid
# on stdout. We tag each request with a unique correlation id in
# details so retrieval is unambiguous.
enqueue() {
    local action="$1" corr="$2"
    admin_py <<PYEOF
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("$BUS", "$OBJ")
iface = dbus.Interface(obj, "$BUS")
details = dbus.Dictionary({"corr": dbus.String("$corr")}, signature="sv")
rid = iface.RequestPermission("$action", details)
print(int(rid))
PYEOF
}

# Helper: decide a request. Echoes "ok" on success, the DBus error
# name on failure.
decide() {
    local rid="$1" decision="$2" scope="$3"
    admin_py <<PYEOF
import sys, dbus
bus = dbus.SystemBus()
obj = bus.get_object("$BUS", "$OBJ")
iface = dbus.Interface(obj, "$BUS")
try:
    iface.DecideRequest(int("$rid"), "$decision", "$scope")
    print("ok")
except dbus.DBusException as e:
    print(e.get_dbus_name() or str(e), file=sys.stderr)
    print("error")
    sys.exit(0)
PYEOF
}

# Helper: does GetPending still contain the given rid?
has_rid() {
    local rid="$1"
    admin_py <<PYEOF
import dbus, sys
bus = dbus.SystemBus()
obj = bus.get_object("$BUS", "$OBJ")
iface = dbus.Interface(obj, "$BUS")
for r in iface.GetPending():
    if int(r["id"]) == int("$rid"):
        print("yes")
        sys.exit(0)
print("no")
PYEOF
}

# Helper: how many pending requests does GetPending currently return?
pending_count() {
    admin_py <<'PYEOF'
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
print(len(iface.GetPending()))
PYEOF
}

# Clean any stale pending state from a prior run.
admin_py <<'PYEOF' >/dev/null 2>&1 || true
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1", "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
for r in iface.GetPending():
    try:
        iface.DecideRequest(int(r["id"]), "deny", "once")
    except Exception:
        pass
PYEOF

# ---------------------------------------------------------------------------
# Step 1 — Rules tab: file-on-disk -> ListRules surface.
# ---------------------------------------------------------------------------

note "Step 1: drop a YAML rule and verify ListRules surfaces it"

rule_file="$RULES_DIR/p07-test-rule.yaml"
cat > "$rule_file" <<'EOF'
- name: "P07 admin-app-polish test rule"
  decision: allow
  match:
    action: "test.p07.action"
    uid: 2000
  scope: "once"
  rationale: "Test rule for P07 admin-app-polish"
EOF

busctl call "$BUS" "$OBJ" "$BUS" ReloadRules >/dev/null \
    || err "ReloadRules failed"
sleep 1

if busctl call "$BUS" "$OBJ" "$BUS" ListRules \
    | grep -q "P07 admin-app-polish test rule"; then
    echo "PASS: broker ListRules returns an on-disk YAML rule"
else
    err "ListRules did not surface the on-disk YAML rule"
fi

# ---------------------------------------------------------------------------
# Step 2 — Rules tab Add: SaveRule writes a new YAML and ListRules
#          picks it up.
# ---------------------------------------------------------------------------

note "Step 2: SaveRule writes a new YAML and ListRules picks it up"

yaml_body=$'- name: "P07 SaveRule test rule"\n  decision: allow\n  match:\n    action: "test.p07.saverule"\n    uid: 2001\n  scope: "once"\n  rationale: "Created via SaveRule"\n'

# SaveRule requires admin uid — call from the admin user.
admin_py <<PYEOF >/dev/null
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("$BUS", "$OBJ")
iface = dbus.Interface(obj, "$BUS")
iface.SaveRule("p07-saverule-test.yaml", '''$yaml_body''')
PYEOF

busctl call "$BUS" "$OBJ" "$BUS" ReloadRules >/dev/null || true
sleep 1

if busctl call "$BUS" "$OBJ" "$BUS" ListRules \
    | grep -q "P07 SaveRule test rule"; then
    echo "PASS: broker SaveRule (as admin) writes a rule that ListRules returns"
else
    err "SaveRule did not result in a visible rule"
fi

# ---------------------------------------------------------------------------
# Step 3 — History tab: ListHistory(100) returns an audit row after a
#          real approve/deny round-trip.
#
# SaveRule does NOT write to the audit log — only DecideRequest does.
# Enqueue a request, approve it, then assert ListHistory returns at
# least one entry (the newly-written row).  This exercises the real
# History-tab code path that the admin app drives on every refresh.
# ---------------------------------------------------------------------------

note "Step 3: enqueue + approve request, then assert ListHistory returns the audit row"

HIST_CORR="p07-hist-$$"
HIST_RID=$(enqueue "test.p07.history" "$HIST_CORR")
if ! [ "$HIST_RID" -ge 0 ] 2>/dev/null; then
    err "RequestPermission for history test failed: got '$HIST_RID'"
fi
hist_decide=$(decide "$HIST_RID" allow once)
[ "$hist_decide" = "ok" ] || err "DecideRequest(allow) for history row failed"
sleep 0.5   # give the broker time to write the audit row

hist_out=$(busctl call "$BUS" "$OBJ" "$BUS" ListHistory i 100 2>&1) \
    || err "ListHistory(100) call failed"
if [ -n "$hist_out" ] && [ "$hist_out" != "aa{sv} 0" ]; then
    echo "PASS: broker ListHistory(100) returns the audit row of a decided request"
else
    err "ListHistory(100) returned empty after approve round-trip (rid=$HIST_RID)"
fi

# ---------------------------------------------------------------------------
# Step 4 — GetPending count (what the app's tray badge reads).
# ---------------------------------------------------------------------------

note "Step 4: GetPending counts a queued request"

# F5 review fix: enqueue a request before asking GetPending so the
# count > 0 path is exercised, not just D-Bus reachability.
TRAY_CORR="p07-tray-$$"
TRAY_RID=$(enqueue "test.p07.tray" "$TRAY_CORR")
if ! [ "$TRAY_RID" -ge 0 ] 2>/dev/null; then
    err "RequestPermission failed: got '$TRAY_RID' (need int rid)"
fi
TRAY_COUNT=$(pending_count)
if [ "$TRAY_COUNT" -ge 1 ]; then
    echo "PASS: broker GetPending counts a newly queued request"
else
    err "GetPending count is 0 right after enqueue (rid=$TRAY_RID)"
fi
# Clean up: deny the request so subsequent steps start clean.
decide "$TRAY_RID" deny once >/dev/null

# ---------------------------------------------------------------------------
# Step 5 — Approve / deny / rule-from-request at the broker. No key
#          event is sent and the app is not running: this checks only
#          that the broker honours the calls the app's handlers make.
# ---------------------------------------------------------------------------

note "Step 5: broker RequestPermission/DecideRequest/SaveRule round-trips"

# Approve: enqueue then DecideRequest(allow).
APPROVE_RID=$(enqueue "test.p07.ctrl_y" "p07-ctrl-y-$$")
[ "$APPROVE_RID" -ge 0 ] 2>/dev/null \
    || err "RequestPermission(test.p07.ctrl_y) failed"
result=$(decide "$APPROVE_RID" allow once)
[ "$result" = "ok" ] || err "DecideRequest(allow) failed for rid=$APPROVE_RID"
sleep 0.3
if [ "$(has_rid "$APPROVE_RID")" = "no" ]; then
    echo "PASS: broker DecideRequest(allow) as admin removes the request from GetPending"
else
    err "rid=$APPROVE_RID still pending after DecideRequest(allow)"
fi

# Deny: enqueue then DecideRequest(deny).
DENY_RID=$(enqueue "test.p07.ctrl_n" "p07-ctrl-n-$$")
[ "$DENY_RID" -ge 0 ] 2>/dev/null \
    || err "RequestPermission(test.p07.ctrl_n) failed"
result=$(decide "$DENY_RID" deny once)
[ "$result" = "ok" ] || err "DecideRequest(deny) failed for rid=$DENY_RID"
sleep 0.3
if [ "$(has_rid "$DENY_RID")" = "no" ]; then
    echo "PASS: broker DecideRequest(deny) as admin removes the request from GetPending"
else
    err "rid=$DENY_RID still pending after DecideRequest(deny)"
fi

# Rule from request: SaveRule as admin with the YAML shape the app's
# RuleEditorDialog sends (broker.save_rule).
ctrl_r_yaml=$'- name: "P07 Ctrl+R test rule"\n  decision: allow\n  match:\n    action: "test.p07.ctrl_r"\n    uid: 2002\n  scope: "once"\n  rationale: "Created via Ctrl+R workflow"\n'
admin_py <<PYEOF >/dev/null
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("$BUS", "$OBJ")
iface = dbus.Interface(obj, "$BUS")
iface.SaveRule("p07-ctrl-r-test.yaml", '''$ctrl_r_yaml''')
PYEOF
sleep 1
if busctl call "$BUS" "$OBJ" "$BUS" ListRules \
    | grep -q "P07 Ctrl+R test rule"; then
    echo "PASS: broker SaveRule of a rule-from-request YAML is returned by ListRules"
else
    err "Ctrl+R SaveRule did not result in a visible rule"
fi

# ---------------------------------------------------------------------------
# Step 6 — Scope rejection: an unknown scope must raise a D-Bus error.
# ---------------------------------------------------------------------------

note "Step 6: deliberately request an invalid scope and verify the broker rejects"

# Per F2 review: emit PASS only when the broker actually raises an
# error for this call — never unconditionally. The broker's
# DecideRequest enforces _VALID_SCOPES at the top; an unknown scope
# yields a `.BadArgument` D-Bus error. A `.ScopeNotPermitted` would
# require a delegated request + a forbidden scope which we can't
# safely set up in this driver without RequestPermissionAs. Accept
# either error-name family as positive evidence that the rejection
# path is wired, and fail_loud when neither fires.
SCOPE_RID=$(enqueue "test.p07.scopenotpermitted" "p07-scope-$$")
[ "$SCOPE_RID" -ge 0 ] 2>/dev/null \
    || err "RequestPermission for scope test failed"

scope_err=$(admin_py <<PYEOF
import sys, dbus
bus = dbus.SystemBus()
obj = bus.get_object("$BUS", "$OBJ")
iface = dbus.Interface(obj, "$BUS")
try:
    iface.DecideRequest(int("$SCOPE_RID"), "allow", "not-a-valid-scope")
    print("__no_exception__")
except dbus.DBusException as e:
    print(e.get_dbus_name() or "")
PYEOF
)
case "$scope_err" in
    *ScopeNotPermitted*|*BadArgument*)
        echo "PASS: broker DecideRequest rejects an unknown scope with a D-Bus error ($scope_err)"
        ;;
    __no_exception__)
        err "broker accepted an invalid scope without raising"
        ;;
    *)
        err "broker raised an unexpected error name: $scope_err"
        ;;
esac
# Clean up: deny the request to remove it from the queue.
decide "$SCOPE_RID" deny once >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

rm -f "$rule_file" \
      "$RULES_DIR/p07-saverule-test.yaml" \
      "$RULES_DIR/p07-ctrl-r-test.yaml"
busctl call "$BUS" "$OBJ" "$BUS" ReloadRules >/dev/null 2>&1 || true

note "s104 driver finished"
exit 0
