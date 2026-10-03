#!/bin/bash
# dev-only (astra+fable A r1, not a DONE-bar driver): the sol A-iii r4 P1
# on the real system. A live launch of silo r4b whose control record's unit=
# is rewritten to a valid but dead unit; --reap-stale must refuse and leave
# the launch running. The labelled-container variant (record removed, label
# naming the dead unit) is covered by the host unit tests only: podman labels
# are immutable, so it cannot be staged on a live launch here.
set -u
T3S_TAG=r4
. "$(dirname "$0")/tier3s-guest-lib.sh"
sm CreateTier3sSilo ssss r4b headless-smoke r4b none > /dev/null
set_argv "r4b=600" | sed 's/^/    /'
TB=$(up_silo r4b); UB=$(unit_of r4b); CB=$(ctr_of r4b)
if [ -n "$TB" ]; then pass "live launch $TB of $UB"; else fail "launch did not come up"; finish; fi
sed -i "s/^unit=.*/unit=qdistro-tier3s-silo@old.service/" "$CTL/$TB/state"
is "record now names the dead old.service" "$(rec "$TB" unit):$(unit_state qdistro-tier3s-silo@old.service)" "qdistro-tier3s-silo@old.service:inactive"
out=$("$CLEANUP" --reap-stale 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/    cleanup: /'
if [ "$rc" -ne 0 ]; then pass "stale record: --reap-stale refuses (rc=$rc)"; else fail "stale record: --reap-stale returned 0"; fi
is "stale record: refusal names the real owner" "$(printf '%s\n' "$out" | grep -c "is bound to '$UB', not to qdistro-tier3s-silo@old.service")" 1
is "stale record: the launch is untouched" "$(ctr_status "$CB"):$(unit_state "qdistro-tier3s-$TB.scope"):$(unit_state "$UB")" "running:active:active"
sed -i "s/^unit=.*/unit=$UB/" "$CTL/$TB/state"
sm StopSilo si r4b 10 > /dev/null; is "StopSilo afterwards" "$(silo_state r4b)" Stopped
assert_launch_gone r4-stop "$TB" "$CB"
sm DeleteSilo s r4b > /dev/null
assert_all_clear r4-end
finish
