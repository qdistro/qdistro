#!/bin/bash
# s123-perm-gates — headless replacement for permissions-gui 36..42
# (clipboard transfer/receive gates, handoff activation gate, ListRules
# surface, SaveRule validation). These scenarios were all `qci:visual: none`:
# every assertion was a broker reply, an audit row or a rules.d file.
# Runs INSIDE the test VM as root, staged by permissions-headless.bats.
#
#   bash s123-perm-gates.sh pg36|pg37|pg38|pg39|pg40|pg41|pg42

. "$(dirname "$0")/s120-perm-lib.sh"

WORKU=$(ensure_uid_user work 2000)

# Rows newer than a mark, so a reused VM's older rows can never satisfy a check.
audit_mark() { sql_audit "SELECT coalesce(max(id),0) FROM audit"; }
# last_audit <mark> <action> <cols-expr> — newest matching row after <mark>.
last_audit() { sql_audit "SELECT $3 FROM audit WHERE id > $1 AND action='$2' ORDER BY id DESC LIMIT 1"; }

xfer() {  # <src> <dst> <mime> [verified=false] [pid=0] [starttime=0]
    bcall_as admin CheckClipboardTransfer ssassssbut "$1" "$2" 1 "$3" "" "" "" \
        "${4:-false}" "${5:-0}" "${6:-0}"
}
recv() {  # <src> <dst> <mime> [verified=false]
    bcall_as admin CheckClipboardReceive ssssssbut "$1" "$2" "$3" "" "" "" "${4:-false}" 0 0
}
handoff() {  # <src> <dst> <src_app> <dst_app> [verified=false]
    bcall_as admin CheckHandoffActivation sssssbut "$1" "$2" "$3" "$4" "" "${5:-false}" 0 0
}
# has_fields <string> <substr>... — every substring present (unordered).
has_fields() { local s=$1 f; shift; for f in "$@"; do [[ $s == *"$f"* ]] || return 1; done; }

# --- pg36: clipboard transfer same-silo allow ---------------------------------
pg36() {
    local m out src r
    isolate_rules
    m=$(audit_mark)
    out=$(xfer user1 user1 text/plain true)
    expect_eq "pg36: same-silo CheckClipboardTransfer(user1,user1) returns allow" "$out" 's "allow"'
    r=$(last_audit "$m" qdistro.clipboard.transfer:user1:user1 "decision||'|'||source")
    if [[ $r == 1\|clipboard_same_silo* ]] && has_fields "$r" "mime=text/plain"; then
        pass "pg36: audit row decision 1, source clipboard_same_silo* with mime=text/plain"
    else
        fail "pg36: audit row unexpected: '$r'"
    fi
    r=$(save_rule "36-rule.yaml" "- name: pg36-deny-user1
  decision: deny
  match:
    action: qdistro.clipboard.transfer:user1:user1
  rationale: should never fire, broker short-circuits same-silo
")
    expect_eq "pg36: deny rule for user1->user1 installed" "$r" "OK $RULES_DIR/36-rule.yaml"
    out=$(xfer user1 user1 text/plain true)
    expect_eq "pg36: same-silo short-circuit ignores the deny rule: still allow" "$out" 's "allow"'
}

# --- pg37: clipboard transfer cross-silo default deny + directional rule ------
pg37() {
    local m out r a=qdistro.clipboard.transfer:user1:admin
    isolate_rules
    m=$(audit_mark)
    out=$(xfer user1 admin text/plain)
    expect_eq "pg37: cross-silo with no rule: deny" "$out" 's "deny"'
    r=$(last_audit "$m" "$a" "decision||'|'||source")
    if [[ $r == 0\|clipboard_default_deny* ]] && has_fields "$r" "mime=text/plain"; then
        pass "pg37: audit decision 0, source clipboard_default_deny* with mime=text/plain"
    else
        fail "pg37: default-deny audit row unexpected: '$r'"
    fi
    r=$(save_rule "37-allow-user1-admin.yaml" "- name: allow-user1-to-admin-clipboard
  decision: allow
  match:
    action: $a
  rationale: pg37 opt-in cross-silo clipboard
")
    expect_eq "pg37: allow rule installed" "$r" "OK $RULES_DIR/37-allow-user1-admin.yaml"
    m=$(audit_mark)
    out=$(xfer user1 admin text/plain)
    expect_eq "pg37: with the rule user1->admin: allow" "$out" 's "allow"'
    r=$(last_audit "$m" "$a" "decision||'|'||rule_path||'|'||source")
    if [[ $r == "1|$RULES_DIR/37-allow-user1-admin.yaml|clipboard_rule"* ]]; then
        pass "pg37: audit decision 1, source clipboard_rule*, rule_path = the rule file"
    else
        fail "pg37: rule audit row unexpected: '$r'"
    fi
    out=$(xfer admin user1 text/plain)
    expect_eq "pg37: rules are directional: admin->user1 still deny" "$out" 's "deny"'
}

# --- pg38: ListRules surface + admin-only ------------------------------------
pg38() {
    local r lr out
    isolate_rules
    cat >"$RULES_DIR/38a-narrow.yaml" <<'YAML'
- name: narrow-py
  decision: allow
  match: {uid: 2000, action: scenario38.narrow, exe: /usr/bin/python3.14}
  rationale: narrow rule — all selectors set
YAML
    cat >"$RULES_DIR/38b-action-only.yaml" <<'YAML'
- name: action-only
  decision: deny
  match: {action: scenario38.action-only}
  rationale: action-only — uid/exe are wildcards
YAML
    cat >"$RULES_DIR/38c-glob.yaml" <<'YAML'
- name: glob-python
  decision: allow
  match: {action: scenario38.glob, exe: "/usr/bin/python*"}
  rationale: glob exe — uid is wildcard
YAML
    r=$(reload_rules)
    expect_eq "pg38: ReloadRules loads 3 rules with no errors" "$r" "OK [3, []]"
    lr=$(broker_call_as admin ListRules '[]')
    out=$(reply_json "$lr" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
want = [
  dict(name="narrow-py", decision="allow", action="scenario38.narrow",
       exe="/usr/bin/python3.14", uid=2000, f="38a-narrow.yaml",
       rationale="narrow rule — all selectors set"),
  dict(name="action-only", decision="deny", action="scenario38.action-only",
       exe="", uid=-1, f="38b-action-only.yaml",
       rationale="action-only — uid/exe are wildcards"),
  dict(name="glob-python", decision="allow", action="scenario38.glob",
       exe="/usr/bin/python*", uid=-1, f="38c-glob.yaml",
       rationale="glob exe — uid is wildcard"),
]
bad = []
if len(rows) != 3: bad.append(f"count={len(rows)}")
for w in want:
    f = w.pop("f")
    m = [r for r in rows if all(r.get(k) == v for k, v in w.items())
         and str(r.get("source_path", "")).endswith("/" + f)]
    if len(m) != 1: bad.append(w["name"])
print("ok" if not bad else "bad:" + ",".join(bad))')
    expect_eq "pg38: ListRules returns exactly the 3 dicts (name/decision/action/exe/uid sentinel -1/exe \"\" sentinel/source_path/rationale)" "$out" ok
    [ "$out" = ok ] || note "ListRules reply: $lr"
    out=$(dsend_as "$WORKU" ListRules)
    if grep -qE 'org\.qdistro\.AdminBroker1\.AccessDenied|org\.freedesktop\.DBus\.Error\.AccessDenied' <<<"$out"; then
        pass "pg38: non-admin (work) ListRules is refused with AccessDenied"
    else
        fail "pg38: work ListRules was not refused: $out"
    fi
}

# --- pg39: SaveRule validation --------------------------------------------------
pg39() {
    local pre post r
    isolate_rules
    r=$(save_rule "39-good.yaml" "- name: pg39-good
  decision: allow
  match: {action: pg39.good}
")
    expect_eq "pg39: baseline good rule installed" "$r" "OK $RULES_DIR/39-good.yaml"
    pre=$(reply_json "$(broker_call_as admin ListRules '[]')" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
    r=$(save_rule "../39-traversal.yaml" "- name: x
  decision: allow
  match: {action: x}
")
    check "pg39: traversal filename refused with RulesEngineRefused" \
        grep -q "^ERR org.qdistro.AdminBroker1.RulesEngineRefused" <<<"$r"
    check "pg39: no 39-traversal.yaml written anywhere under /etc" \
        bash -c '[ -z "$(find /etc -name "39-traversal.yaml" 2>/dev/null)" ]'
    r=$(save_rule "39-bad-yaml.yaml" "- name: x
  decision: allow
  match: {action: : [unbalanced")
    check "pg39: invalid YAML refused with RulesEngineRefused" \
        grep -q "^ERR org.qdistro.AdminBroker1.RulesEngineRefused" <<<"$r"
    check "pg39: no 39-bad-yaml.yaml on disk" test ! -e "$RULES_DIR/39-bad-yaml.yaml"
    r=$(save_rule "39-bad-shape.yaml" "name: x
decision: allow
match:
  action: x
")
    check "pg39: dict-not-list schema refused with RulesEngineRefused" \
        grep -q "^ERR org.qdistro.AdminBroker1.RulesEngineRefused" <<<"$r"
    check "pg39: no 39-bad-shape.yaml on disk" test ! -e "$RULES_DIR/39-bad-shape.yaml"
    post=$(reply_json "$(broker_call_as admin ListRules '[]')" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
    expect_eq "pg39: ListRules count unchanged by the refused saves" "$post" "$pre"
}

# --- pg40 (40-clipboard-receive-same-silo) ------------------------------------
pg40() {
    local m out r
    isolate_rules
    m=$(audit_mark)
    out=$(recv user1 user1 text/plain true)
    expect_eq "pg40: same-silo CheckClipboardReceive returns allow" "$out" 's "allow"'
    r=$(last_audit "$m" qdistro.clipboard.receive:user1:user1 "decision||'|'||source")
    if [[ $r == 1\|* ]] && has_fields "$r" clipboard_receive_same_silo mime=text/plain; then
        pass "pg40: audit decision 1, source contains clipboard_receive_same_silo and mime=text/plain"
    else
        fail "pg40: audit row unexpected: '$r'"
    fi
    r=$(save_rule "40-rule.yaml" "- name: pg40-deny-user1-receive
  decision: deny
  match:
    action: qdistro.clipboard.receive:user1:user1
")
    expect_eq "pg40: deny rule installed" "$r" "OK $RULES_DIR/40-rule.yaml"
    out=$(recv user1 user1 text/plain true)
    expect_eq "pg40: same-silo receive short-circuit ignores the deny rule" "$out" 's "allow"'
}

# --- pg41: clipboard receive per-MIME glob ----------------------------------
pg41() {
    local m out r a=qdistro.clipboard.receive:user1:admin
    isolate_rules
    m=$(audit_mark)
    out=$(recv user1 admin text/plain)
    expect_eq "pg41: cross-silo receive, no rule: deny" "$out" 's "deny"'
    r=$(last_audit "$m" "$a" "decision||'|'||source")
    if [[ $r == 0\|clipboard_receive_default_deny* ]] && has_fields "$r" mime=text/plain; then
        pass "pg41: audit decision 0, source clipboard_receive_default_deny* mime=text/plain"
    else
        fail "pg41: default-deny audit row unexpected: '$r'"
    fi
    r=$(save_rule "41a-allow.yaml" "- name: allow-user1-to-admin-receive
  decision: allow
  match:
    action: $a
")
    expect_eq "pg41: wildcard-mime allow rule installed" "$r" "OK $RULES_DIR/41a-allow.yaml"
    m=$(audit_mark)
    expect_eq "pg41: wildcard rule: text/plain allow" "$(recv user1 admin text/plain)" 's "allow"'
    expect_eq "pg41: wildcard rule: image/png allow" "$(recv user1 admin image/png)" 's "allow"'
    r=$(sql_audit "SELECT group_concat(decision||'|'||source, '~') FROM (SELECT decision, source FROM audit WHERE id > $m AND action='$a' ORDER BY id)")
    if has_fields "$r" "1|" clipboard_receive_rule mime=text/plain mime=image/png; then
        pass "pg41: two rule-allow audit rows keep the concrete MIME values"
    else
        fail "pg41: wildcard-rule audit rows unexpected: '$r'"
    fi
    rm -f "$RULES_DIR/41a-allow.yaml"
    r=$(save_rule "41b-text-only.yaml" "- name: allow-user1-to-admin-text-only
  decision: allow
  match:
    action: $a
    mime_type: text/*
")
    expect_eq "pg41: text/* rule installed (wildcard rule removed)" "$r" "OK $RULES_DIR/41b-text-only.yaml"
    m=$(audit_mark)
    expect_eq "pg41: text/* rule: text/plain allow" "$(recv user1 admin text/plain)" 's "allow"'
    expect_eq "pg41: text/* rule: text/html allow" "$(recv user1 admin text/html)" 's "allow"'
    expect_eq "pg41: text/* rule: image/png deny" "$(recv user1 admin image/png)" 's "deny"'
    expect_eq "pg41: text/* rule: application/pdf deny" "$(recv user1 admin application/pdf)" 's "deny"'
    r=$(python3 - "$m" "$a" <<'PY'
import sqlite3, sys
rows = sqlite3.connect("/var/lib/qdistro/audit/audit.sqlite").execute(
    "SELECT decision, source FROM audit WHERE id > ? AND action = ? ORDER BY id",
    (int(sys.argv[1]), sys.argv[2])).fetchall()
exp = [(1, "clipboard_receive_rule", "mime=text/plain"),
       (1, "clipboard_receive_rule", "mime=text/html"),
       (0, "clipboard_receive_default_deny", "mime=image/png"),
       (0, "clipboard_receive_default_deny", "mime=application/pdf")]
ok = len(rows) == 4 and all(d == e[0] and e[1] in s and e[2] in s
                            for (d, s), e in zip(rows, exp))
print("ok" if ok else repr(rows))
PY
)
    expect_eq "pg41: audit: 2 rule allows (text/plain,text/html) then 2 default denies (image/png,application/pdf)" "$r" ok
}

# --- pg42: handoff activation gate --------------------------------------------
pg42() {
    local m out r a=qdistro.handoff.activate:user1:admin
    isolate_rules
    m=$(audit_mark)
    out=$(handoff user1 user1 org.mozilla.firefox org.mozilla.firefox true)
    expect_eq "pg42: same-silo handoff: allow" "$out" 's "allow"'
    r=$(last_audit "$m" qdistro.handoff.activate:user1:user1 "decision||'|'||source")
    if [[ $r == "1|handoff_same_silo_verified "* ]] && has_fields "$r" src_app=org.mozilla.firefox; then
        pass "pg42: audit 1|handoff_same_silo_verified ... src_app=org.mozilla.firefox"
    else
        fail "pg42: same-silo audit row unexpected: '$r'"
    fi
    m=$(audit_mark)
    out=$(handoff user1 admin org.mozilla.firefox "")
    expect_eq "pg42: cross-silo, no rule: deny" "$out" 's "deny"'
    r=$(last_audit "$m" "$a" "decision||'|'||substr(source,1,32)")
    expect_eq "pg42: audit 0|handoff_default_deny secctx_prov" "$r" "0|handoff_default_deny secctx_prov"
    r=$(save_rule "42-allow-firefox.yaml" "- name: allow-firefox-handoff-user1-to-admin
  decision: allow
  match:
    action: $a
    app_id: org.mozilla.firefox
  rationale: pg42 per-app handoff opt-in
")
    expect_eq "pg42: SaveRule returns the installed path" "$r" "OK $RULES_DIR/42-allow-firefox.yaml"
    m=$(audit_mark)
    expect_eq "pg42: app_id rule: firefox allow" "$(handoff user1 admin org.mozilla.firefox "")" 's "allow"'
    expect_eq "pg42: app_id rule: chrome deny" "$(handoff user1 admin com.google.Chrome "")" 's "deny"'
    r=$(sql_audit "SELECT group_concat(x, '~') FROM (SELECT decision||'|'||substr(source,1,32)||'|'||coalesce(rule_path,'') AS x FROM audit WHERE id > $m AND action='$a' ORDER BY id DESC)")
    expect_eq "pg42: audit: chrome default-deny, firefox rule allow with rule_path" "$r" \
        "0|handoff_default_deny secctx_prov|~1|handoff_rule secctx_provenance=l|$RULES_DIR/42-allow-firefox.yaml"
    out=$(dsend_as "$WORKU" CheckHandoffActivation string:user1 string:admin string:x string:"" string:"")
    if grep -qE 'org\.qdistro\.AdminBroker1\.AccessDenied|org\.freedesktop\.DBus\.Error\.AccessDenied' <<<"$out"; then
        pass "pg42: non-admin (work) CheckHandoffActivation refused with AccessDenied"
    else
        fail "pg42: work CheckHandoffActivation was not refused: $out"
    fi
}

case "${1:-}" in
    pg36|pg37|pg38|pg39|pg40|pg41|pg42) "$1" ;;
    *) die "usage: $0 pg36|pg37|pg38|pg39|pg40|pg41|pg42" ;;
esac
[ "$_S120_FAILED" = 0 ] && printf 'PASS: s123 %s complete\n' "$1"
