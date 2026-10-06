#!/bin/bash
# s131-tier3s-netnone.sh — GUEST driver (root) for phase7-tier3s-netnone.bats.
# Tier 3s Phase C (todo/paravirt 10, README O3): `network=none` is enforced,
# not just requested — proven INSIDE the sandbox (gVisor's own view), not
# only from podman's config:
#   - the sandbox sees exactly one link: lo
#   - loopback itself is up and addressed (v4 127.0.0.1 + v6 ::1)
#   - no non-loopback route exists in any table; no default route
#   - `ip route get <non-loopback test addr>` reports unreachable
#   - a TCP connect to a non-loopback test address fails with an explicit
#     ENETUNREACH diagnostic, fast — not a packet-filter timeout (a
#     timeout could hide a routed path); 192.0.2.1 is TEST-NET-1, a
#     documentation address that is never assigned, not "public"
#   - host-side corroboration: the container's NetworkMode is `none` and the
#     runsc cmdline carries --network=none (runsc's own stack off, not just
#     podman's)
# Runs after tier3s-guest-setup.sh. Each check prints one PASS/FAIL line;
# `[s131] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s131
. "$(dirname "$0")/tier3s-guest-lib.sh"
SN=s131n

step "0. preconditions (setup ran)"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "probe PASS before the launch" "$rc:$(printf '%s\n' "$out" | grep -c '^RESULT PASS')" "0:1"
is "image present" "$(yes_no pm image exists "$IMAGE")" yes
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow
assert_all_clear pre
sm CreateTier3sSilo ssss "$SN" headless-smoke "$SN" none > /dev/null
is "CreateTier3sSilo $SN" "$(silo_state "$SN")" Created
set_argv "$SN=600" | sed 's/^/    /'
is "argv set with the manager restarted" "$(yes_no manager_up)" yes

step "1. live silo: network=none inside the sandbox"
TN=$(up_silo $SN); CN=$(ctr_of $SN)
if [ -n "$TN" ]; then pass "silo $SN $TN recorded running"; else fail "silo did not come up"; finish; fi
sx() { pm exec "$CN" "$@"; }   # sx <in-sandbox command...>

# --- links: lo is the only one
is "netnone: the only link is lo" \
    "$(sx ip -o link show 2>/dev/null | cut -d: -f2 | tr -d ' ' | tr '\n' ',')" "lo,"
is "netnone: lo is UP" "$(sx ip -o link show lo 2>/dev/null | grep -c 'UP')" 1

# --- addresses: only loopback scopes on the only link
addr4=$(sx ip -o -4 addr show 2>/dev/null | tr -s ' ' | sed 's/^ //')
addr6=$(sx ip -o -6 addr show 2>/dev/null | tr -s ' ' | sed 's/^ //')
printf '%s\n' "$addr4" "$addr6" | sed 's/^/    addr: /'
is "netnone: v4 loopback address present" "$(printf '%s\n' "$addr4" | grep -c '127\.0\.0\.1')" 1
is "netnone: v6 loopback address present" "$(printf '%s\n' "$addr6" | grep -c '::1')" 1
is "netnone: no non-loopback v4 address" "$(printf '%s\n' "$addr4" | grep -vc '127\.0\.0\|^$')" 0
is "netnone: no non-loopback v6 address" "$(printf '%s\n' "$addr6" | grep -vc '::1\|^$')" 0

# --- routes: loopback only, no default, nothing elsewhere in any table
rt=$(sx ip -o route show table all 2>/dev/null | tr -s ' ')
printf '%s\n' "$rt" | sed 's/^/    route: /'
is "netnone: no default route" "$(printf '%s\n' "$rt" | grep -c '^default')" 0
is "netnone: every route is on lo" \
    "$(printf '%s\n' "$rt" | grep . | grep -vc ' dev lo')" 0
rg=$(sx ip route get 192.0.2.1 2>&1); rg_rc=$?
info "ip route get 192.0.2.1: rc=$rg_rc, out: $rg"
# rc alone is not the proof — the command must have RUN and reported
# unreachable, not failed for another reason (sol r1 P3)
if [ "$rg_rc" -ne 0 ] && printf '%s' "$rg" | grep -qi 'unreachable'; then
    pass "netnone: route to the non-loopback test address is unreachable (rc=$rg_rc)"
else
    fail "netnone: 'ip route get 192.0.2.1' did not report unreachable: rc=$rg_rc, $rg"
fi

# --- a real connect attempt fails with an explicit ENETUNREACH diagnostic,
# fast: that is proof of no path. A nonzero rc alone could be a refused
# connection (a routed path) or the probe not running at all (sol r1 P3).
t0=$SECONDS
out2=$(pm exec "$CN" bash -c 'timeout 15 bash -c "exec 3<>/dev/tcp/192.0.2.1/80" 2>&1; echo rc=$?'); dt=$((SECONDS - t0))
info "connect 192.0.2.1:80: ${dt}s, output: $(printf '%s' "$out2" | tr '\n' '|')"
is "netnone: the connect probe ran and failed" "$(printf '%s\n' "$out2" | grep -c 'rc=[1-9]')" 1
is "netnone: the failure is ENETUNREACH, not a refusal or a probe error" \
    "$(printf '%s' "$out2" | grep -icE 'network.{0,10}unreachable|ENETUNREACH|errno.{0,4}101' | sed 's/[1-9][0-9]*/yes/')" yes
is "netnone: the failure is fast (not a filtered-route timeout)" \
    "$([ "$dt" -lt 14 ] && echo yes || echo "no (${dt}s)")" yes

# --- host-side corroboration: the podman spec and the runsc cmdline agree
is "netnone: podman NetworkMode" "$(pm inspect --format '{{.HostConfig.NetworkMode}}' "$CN")" none
ipid=$(pm inspect --format '{{.State.Pid}}' "$CN")
is "netnone: sentry runs with --network=none" \
    "$(tr '\0' '\n' < "/proc/$ipid/cmdline" 2>/dev/null | grep -cx -- '--network=none')" 1

step "2. teardown + all clear"
sm StopSilo si $SN 10 > /dev/null; is "StopSilo rc" "$?" 0
assert_launch_gone netnone "$TN" "$CN"
sm DeleteSilo s "$SN" > /dev/null; is "DeleteSilo" "$(silo_state "$SN")" absent
assert_all_clear end
finish
