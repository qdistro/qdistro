#!/bin/bash
# s132-tier3s-provfrag.sh — GUEST driver (root) for phase7-tier3s-provfrag.bats.
# Tier 3s provision-fragment repair (todo/paravirt PLAN follow-up): a launch
# unit killed mid-useradd — the startup reconcile stops every tier3s unit, and
# a stop during activation does the same — leaves a bare qt3s-<silo> account:
# passwd row + GECOS present, no home, no /etc/sub{u,g}id rows. Before the
# repair, that fragment refused every later launch forever ("no subuid/subgid
# rows"). The spawn now deletes and re-provisions exactly that signature:
#   - fragment (no home, no subid rows, no live uid)  -> repair, then the
#     launch proceeds to the next refusal (empty image store), and a
#     subsequent ensure+start reaches Active
#   - partial state (home present, subid rows gone)  -> still refused, account
#     untouched — real state is never deleted
#   - fragment with a LIVE process on the uid        -> still refused, account
#     untouched; once the process dies the next launch repairs
#   - one-sided subid rows / dangling home symlink / a MISSING subid db
#     (grep rc=2, not rc=1)                                -> all refused,
#     account untouched — a lookup error is not "row absent"
# Runs after tier3s-guest-setup.sh. Each check prints one PASS/FAIL line;
# `[s132] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s132
. "$(dirname "$0")/tier3s-guest-lib.sh"
SF=s132frag; SP=s132part; SL=s132live; SO=s132one; SY=s132sym; SE=s132err
AF=$(silo_acct "$SF"); AP=$(silo_acct "$SP"); AL=$(silo_acct "$SL")
AO=$(silo_acct "$SO"); AY=$(silo_acct "$SY"); AE=$(silo_acct "$SE")

# forge_fragment <account> <silo> — the exact state a mid-useradd kill leaves:
# passwd row with the tier3s GECOS, locked password, NO home, NO subid rows.
forge_fragment() {
    local a="$1" s="$2"
    useradd -M -s /bin/bash -G qdistro-tier3s -c "qdistro tier3s silo $s" "$a" \
        && passwd -l "$a" >/dev/null || { echo "forge_fragment: useradd $a failed" >&2; return 1; }
    sed -i "/^$a:/d" /etc/subuid /etc/subgid
    local u h
    u=$(id -u "$a"); h=$(getent passwd "$a" | cut -d: -f6)
    [ ! -e "$h" ] && ! grep -q "^$a:" /etc/subuid && ! grep -q "^$a:" /etc/subgid \
        || { echo "forge_fragment: $a is not a fragment (home=$h)" >&2; return 1; }
    [ "$u" -ge 1000 ]
}

# quiet probes for yes_no — it echoes the command's stdout, so anything
# that prints (id, getent, pgrep) must be silenced inside a function.
acct_exists() { id "$1" >/dev/null 2>&1; }
tier3s_group_ok() { getent group qdistro-tier3s >/dev/null 2>&1; }
uid_live() { pgrep -u "$(id -u "$1")" >/dev/null 2>&1; }

# acct_healthy <account> — post-repair shape: home present and owned, both
# subid rows present.
acct_healthy() {
    local a="$1" u h
    u=$(id -u "$a" 2>/dev/null) || return 1
    h=$(getent passwd "$a" | cut -d: -f6)
    [ -d "$h" ] && [ ! -L "$h" ] && [ "$(stat -c %u -- "$h")" = "$u" ] \
        && grep -q "^$a:[0-9]*:[1-9][0-9]*$" /etc/subuid \
        && grep -q "^$a:[0-9]*:[1-9][0-9]*$" /etc/subgid
}

step "0. preconditions (setup ran)"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "probe PASS before the launches" "$rc:$(printf '%s\n' "$out" | grep -c '^RESULT PASS')" "0:1"
is "image staged in admin's store (archive source)" "$(yes_no pm image exists "$IMAGE")" yes
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow
is "tier3s group present" "$(yes_no tier3s_group_ok)" yes
assert_all_clear pre
for s in $SF $SP $SL $SO $SY $SE; do
    sm CreateTier3sSilo ssss "$s" headless-smoke "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_argv "$SF=600" | sed 's/^/    /'
is "argv set with the manager restarted" "$(yes_no manager_up)" yes

# ---------------------------------------------------------------------------
step "1. killed-mid-useradd fragment: repaired, launch reaches the image check"
unit=$(unit_of "$SF"); cur=$(journal_cursor)
forge_fragment "$AF" "$SF" \
    && pass "fragment forged for $SF (no home, no subid rows)" \
    || { fail "could not forge the $SF fragment"; finish; }
is "fragment: account resolves" "$(yes_no acct_exists "$AF")" yes
is "fragment: no home dir" "$(yes_no test -e "$(getent passwd "$AF" | cut -d: -f6)")" no
is "fragment: no subid rows" \
    "$(grep -c "^$AF:" /etc/subuid):$(grep -c "^$AF:" /etc/subgid)" "0:0"

sm StartSilo s "$SF" > "$WORK/start.sf" 2>&1; rc=$?
info "fragment launch rc=$rc $(tr '\n' ' ' < "$WORK/start.sf" | cut -c1-200)"
if [ "$rc" -ne 0 ]; then pass "fragmented launch fails (rc=$rc — empty store after re-provision)"
else fail "fragmented launch unexpectedly succeeded"; fi
unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
is "spawn repaired the fragment" \
    "$(unit_log "$unit" "$cur" | grep -cF "removed a killed-mid-useradd fragment of $AF")" 1
is "spawn re-provisioned the account" \
    "$(unit_log "$unit" "$cur" | grep -cF "provisioned silo account $AF")" 1
is "launch reached a LATER refusal (the empty silo store), not the fragment" \
    "$(unit_log "$unit" "$cur" | grep -cF "REFUSE: image $IMAGE is not in the silo's podman store")" 1
is "the old wedge refusal is gone" \
    "$(unit_log "$unit" "$cur" | grep -cF "no subuid/subgid rows")" 0
is "account is healthy after repair (home owned, subid rows)" "$(yes_no acct_healthy "$AF")" yes
is "silo reads Stopped after the refused launch" "$(silo_state "$SF")" Stopped
systemctl reset-failed "$unit" 2>/dev/null

step "2. repaired account launches for real"
if ensure_silo_image "$SF" headless-smoke; then pass "$SF: image loaded into the re-provisioned store"
else fail "$SF: ensure_silo_image failed"; fi
TF=$(up_silo "$SF")
if [ -n "$TF" ]; then pass "repaired silo $SF launched ($TF)"; else fail "repaired silo did not come up"; finish; fi
is "silo $SF Active" "$(silo_state "$SF")" Active
sm StopSilo si "$SF" 10 > /dev/null; is "StopSilo $SF" "$(silo_state "$SF")" Stopped
wait_for 60 unit_down "$(unit_of "$SF")"
assert_launch_gone repaired "$TF" "$SF"

# ---------------------------------------------------------------------------
step "3. partial state (home present, subid rows gone) is NOT auto-deleted"
unit=$(unit_of "$SP"); cur=$(journal_cursor)
useradd -m -s /bin/bash -G qdistro-tier3s -c "qdistro tier3s silo $SP" "$AP" \
    && passwd -l "$AP" >/dev/null || { fail "useradd $AP failed"; finish; }
sed -i "/^$AP:/d" /etc/subuid /etc/subgid
is "$SP: home exists, subid rows stripped" \
    "$(yes_no test -d "$(getent passwd "$AP" | cut -d: -f6)"):$(grep -c "^$AP:" /etc/subuid)" "yes:0"
sm StartSilo s "$SP" > "$WORK/start.sp" 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "$SP launch refused (rc=$rc)"; else fail "$SP launch unexpectedly succeeded"; fi
unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
is "$SP: refused on the missing subid rows" \
    "$(unit_log "$unit" "$cur" | grep -cF "REFUSE: silo account $AP has no subuid/subgid rows")" 1
is "$SP: repair did NOT fire (home exists — not a fragment signature)" \
    "$(unit_log "$unit" "$cur" | grep -cF "removed a killed-mid-useradd fragment")" 0
is "$SP: account and home are still there" \
    "$(yes_no acct_exists "$AP"):$(yes_no test -d "$(getent passwd "$AP" | cut -d: -f6)")" "yes:yes"
systemctl reset-failed "$unit" 2>/dev/null
userdel -r "$AP" >/dev/null 2>&1 || userdel -f "$AP" >/dev/null 2>&1 || :

# ---------------------------------------------------------------------------
step "4. fragment with a live process on the uid is NOT deleted"
unit=$(unit_of "$SL"); cur=$(journal_cursor)
forge_fragment "$AL" "$SL" \
    && pass "fragment forged for $SL" \
    || { fail "could not forge the $SL fragment"; finish; }
runuser -u "$AL" -- sleep 120 &
live_pid=$!
wait_for 10 uid_live "$AL" || fail "$SL: the decoy process never came up"
is "$SL: a process runs as the fragment uid" "$(yes_no uid_live "$AL")" yes
sm StartSilo s "$SL" > "$WORK/start.sl" 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "$SL launch refused (rc=$rc)"; else fail "$SL launch unexpectedly succeeded"; fi
unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
is "$SL: refused on the live uid" \
    "$(unit_log "$unit" "$cur" | grep -cF "REFUSE: $AL is a provision fragment but uid")" 1
is "$SL: account not deleted while its uid is live" "$(yes_no acct_exists "$AL")" yes
kill "$live_pid" 2>/dev/null; wait "$live_pid" 2>/dev/null || :
cur=$(journal_cursor)
sm StartSilo s "$SL" > "$WORK/start.sl2" 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "$SL retry refused (rc=$rc — image check, store empty)"
else fail "$SL retry unexpectedly succeeded"; fi
is "$SL: once the process is gone the fragment is repaired" \
    "$(unit_log "$unit" "$cur" | grep -cF "removed a killed-mid-useradd fragment of $AL")" 1
is "$SL: account healthy after repair" "$(yes_no acct_healthy "$AL")" yes
systemctl reset-failed "$unit" 2>/dev/null

# ---------------------------------------------------------------------------
step "5. one-sided subid rows and a dangling-symlink home are NOT repaired"
# 5a: subuid row present, subgid missing — a fragment must have NEITHER row;
# partial rows could be hand-planted, so the account stays refused+untouched.
unit=$(unit_of "$SO"); cur=$(journal_cursor)
forge_fragment "$AO" "$SO" \
    && pass "fragment forged for $SO" \
    || { fail "could not forge the $SO fragment"; finish; }
echo "$AO:196608:65536" >> /etc/subuid
sm StartSilo s "$SO" > "$WORK/start.so" 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "$SO launch refused (rc=$rc)"; else fail "$SO launch unexpectedly succeeded"; fi
unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
is "$SO: refused on the one-sided subid rows" \
    "$(unit_log "$unit" "$cur" | grep -cF "REFUSE: silo account $AO has no subuid/subgid rows")" 1
is "$SO: repair did NOT fire (a subid row exists)" \
    "$(unit_log "$unit" "$cur" | grep -cF "removed a killed-mid-useradd fragment")" 0
is "$SO: account and its planted row are still there" \
    "$(yes_no acct_exists "$AO"):$(grep -c "^$AO:" /etc/subuid)" "yes:1"
systemctl reset-failed "$unit" 2>/dev/null
userdel -f "$AO" >/dev/null 2>&1 || :
sed -i "/^$AO:/d" /etc/subuid /etc/subgid

# 5b: home path is a dangling symlink — "no home" must mean truly absent, so
# -e AND -L are both checked; a planted link keeps the account refused+alive.
unit=$(unit_of "$SY"); cur=$(journal_cursor)
forge_fragment "$AY" "$SY" \
    && pass "fragment forged for $SY" \
    || { fail "could not forge the $SY fragment"; finish; }
hy=$(getent passwd "$AY" | cut -d: -f6)
ln -s /nonexistent-t3s-target "$hy"
is "$SY: home path is a dangling symlink" \
    "$(yes_no test -L "$hy"):$(yes_no test -e "$hy")" "yes:no"
sm StartSilo s "$SY" > "$WORK/start.sy" 2>&1; rc=$?
if [ "$rc" -ne 0 ]; then pass "$SY launch refused (rc=$rc)"; else fail "$SY launch unexpectedly succeeded"; fi
unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
# (sub_ok is checked before home_ok, so the refusal names the subid rows;
# what step 5b proves is that the symlink did NOT count as an absent home)
is "$SY: refused (the symlink is not an absent home)" \
    "$(unit_log "$unit" "$cur" | grep -c "spawn-tier3s: REFUSE:")" 1
is "$SY: repair did NOT fire (the path is not absent)" \
    "$(unit_log "$unit" "$cur" | grep -cF "removed a killed-mid-useradd fragment")" 0
is "$SY: account and planted symlink are still there" \
    "$(yes_no acct_exists "$AY"):$(yes_no test -L "$hy")" "yes:yes"
systemctl reset-failed "$unit" 2>/dev/null
rm -f "$hy"; userdel -f "$AY" >/dev/null 2>&1 || :

# 5c: a FAILED subid lookup must refuse, never delete — rc>=2 is not "row
# absent". The spawn runs as root so a mode-000 db is still readable; the
# failure case a root caller sees is the file gone entirely (grep rc=2).
unit=$(unit_of "$SE"); cur=$(journal_cursor)
forge_fragment "$AE" "$SE" \
    && pass "fragment forged for $SE" \
    || { fail "could not forge the $SE fragment"; finish; }
mv /etc/subuid /etc/subuid.s132-moved
sm StartSilo s "$SE" > "$WORK/start.se" 2>&1; rc=$?
mv /etc/subuid.s132-moved /etc/subuid
if [ "$rc" -ne 0 ]; then pass "$SE launch refused (rc=$rc)"; else fail "$SE launch unexpectedly succeeded"; fi
unit_log "$unit" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'
is "$SE: refusal cites the unreadable subid db" \
    "$(unit_log "$unit" "$cur" | grep -cF "cannot prove $AE has no subid rows")" 1
is "$SE: repair did NOT fire on a lookup error" \
    "$(unit_log "$unit" "$cur" | grep -cF "removed a killed-mid-useradd fragment")" 0
is "$SE: fragment account is still there" "$(yes_no acct_exists "$AE")" yes
systemctl reset-failed "$unit" 2>/dev/null
userdel -f "$AE" >/dev/null 2>&1 || :
assert_all_clear post
finish
