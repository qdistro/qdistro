#!/bin/bash
# In-VM driver for the broker TCB no-network discipline (todo/fable-networking
# task 5). The headless halves live in tests/unit/test_broker_no_network.py;
# this is the VM-gated half that needs a real systemd + selinux-policy-devel.
#
# It proves the TWO enforcement layers task 5 actually relies on:
#
#   1. RUNTIME (systemd) — the real no-network guarantee. Under the broker's
#      unit confinement (RestrictAddressFamilies=AF_UNIX AF_NETLINK +
#      PrivateNetwork=yes + IPAddressDeny=any) an AF_INET socket() fails
#      EAFNOSUPPORT, while AF_UNIX (the broker's actual transport) still works.
#      Demonstrated generically via `systemd-run` so it holds regardless of
#      whether the hardened unit is already installed; if the live unit IS
#      present we also assert its directives directly.
#
#   2. BUILD RATCHET (SELinux) — defence-in-depth. The qdistro_broker.te 0.6.0
#      neverallow pins off EVERY network socket class for qdistro_broker_t
#      (tcp/udp/rawip/sctp/dccp/icmp/netlink_route/netlink_tcpdiag/packet) —
#      satisfiable only because the domain uses auth_read_passwd, not
#      auth_use_pam, so it joins no nsswitch_domain socket grants. We prove the
#      ratchet has teeth: an injected forbidden
#      `allow qdistro_broker_t self:rawip_socket create` is rejected (negative
#      control, run first), and the CLEAN module then loads with ZERO
#      qdistro_broker_t neverallow violations under assertion checking.
#
# CRITICAL subtlety (VM-verified 2026-06-11): module neverallows are checked
# only when semanage `expand-check=1` (the distro default is 0 — assertions are
# NOT checked, which is why the 0.5.0 ratchet was cosmetic). AND on a stock
# Tumbleweed image the BASE policy itself fails ~14 of its own neverallow
# assertions under expand-check=1, so `semodule -i` always exits nonzero. The
# load-bearing signal is therefore the COUNT of `neverallow qdistro_broker_t`
# lines in the semodule output, never the exit code.
#
# State hygiene: backs up /etc/selinux/semanage.conf, flips expand-check, and
# restores it + reinstalls the clean module on EXIT/INT/TERM.
#
# Usage (run as root in the VM):
#   s56b-broker-no-network.sh [BROKER_SELINUX_DIR]
# BROKER_SELINUX_DIR defaults to the first of:
#   /tmp/brk  (HTTP-staged by an operator/bats wrapper)
#   /root/qdistro-src/qdistro/selinux/broker
# and must contain qdistro_broker.{te,if,fc}.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*" >&2; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

[ "$(id -u)" = "0" ] || skip "must run as root"

DEVEL=/usr/share/selinux/devel
SRC="${1:-}"
if [ -z "$SRC" ]; then
    for cand in /tmp/brk /root/qdistro-src/qdistro/selinux/broker; do
        [ -f "$cand/qdistro_broker.te" ] && SRC="$cand" && break
    done
fi
[ -n "$SRC" ] && [ -f "$SRC/qdistro_broker.te" ] \
    || skip "broker SELinux source not found (pass BROKER_SELINUX_DIR)"

# ---------------------------------------------------------------------------
# Layer 1 — systemd runtime no-network (always runnable; no SELinux needed).
# ---------------------------------------------------------------------------
command -v systemd-run >/dev/null 2>&1 || skip "systemd-run absent"

CONF='RestrictAddressFamilies=AF_UNIX AF_NETLINK'
PY_INET='import socket,sys
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    print("INET_OPENED_BAD"); sys.exit(3)
except OSError as e:
    print("AF_INET errno=%d" % e.errno); sys.exit(0 if e.errno in (97,13,1) else 4)'
if systemd-run --quiet --wait \
        -p "$CONF" -p PrivateNetwork=yes -p IPAddressDeny=any \
        /usr/bin/python3 -c "$PY_INET" >/dev/null 2>&1; then
    pass "systemd confinement denies AF_INET socket() (EAFNOSUPPORT) for the broker unit recipe"
else
    fail "AF_INET socket() was NOT denied under RestrictAddressFamilies+PrivateNetwork"
fi

PY_UNIX='import socket; socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)'
if systemd-run --quiet --wait \
        -p "$CONF" -p PrivateNetwork=yes \
        /usr/bin/python3 -c "$PY_UNIX" >/dev/null 2>&1; then
    pass "systemd confinement still permits AF_UNIX (the broker's real transport)"
else
    fail "AF_UNIX socket() was denied under the broker confinement — would break the broker"
fi

# If the hardened unit is actually installed, assert its directives directly.
UNIT=qdistro-admin-broker.service
if systemctl cat "$UNIT" >/dev/null 2>&1; then
    pn=$(systemctl show -p PrivateNetwork --value "$UNIT" 2>/dev/null)
    raf=$(systemctl show -p RestrictAddressFamilies --value "$UNIT" 2>/dev/null)
    if [ "$pn" = "yes" ] && printf '%s' "$raf" | grep -q "AF_UNIX" \
            && ! printf '%s' "$raf" | grep -qE "AF_INET(\b|6)"; then
        pass "live $UNIT carries PrivateNetwork=yes + AF_UNIX-only RestrictAddressFamilies"
    else
        fail "live $UNIT missing no-network hardening (PrivateNetwork=$pn RAF=[$raf])"
    fi
else
    echo "INFO: $UNIT not installed on this image — runtime directive check skipped (recipe proven generically above)"
fi

# ---------------------------------------------------------------------------
# Layer 2 — SELinux build ratchet (needs selinux-policy-devel + checkmodule).
# ---------------------------------------------------------------------------
if ! command -v semodule >/dev/null 2>&1 || [ ! -f "$DEVEL/Makefile" ]; then
    echo "INFO: selinux-policy-devel/semodule absent — SELinux build-ratchet skipped"
    echo "[s56b] $PASSCOUNT passes, $FAILCOUNT failures"
    [ "$FAILCOUNT" -eq 0 ] && exit 0 || exit 1
fi

SEMANAGE_CONF=/etc/selinux/semanage.conf
WORK=$(mktemp -d)
CONF_BAK="$WORK/semanage.conf.bak"
[ -f "$SEMANAGE_CONF" ] && cp "$SEMANAGE_CONF" "$CONF_BAK"

restore() {
    # Put expand-check back the way we found it and reload the clean module so
    # the running system is never left with assertion checking on or a mutated
    # broker policy.
    [ -f "$CONF_BAK" ] && cp "$CONF_BAK" "$SEMANAGE_CONF" 2>/dev/null || true
    if [ -f "$WORK/clean/qdistro_broker.pp" ]; then
        semodule -i "$WORK/clean/qdistro_broker.pp" >/dev/null 2>&1 || true
    fi
    rm -rf "$WORK" 2>/dev/null || true
}
trap restore EXIT INT TERM

# Count of OUR module's neverallow violations in a semodule -i run (the base
# policy contributes its own unrelated failures + a nonzero exit on this image).
broker_neverallow_failures() {  # $1 = build dir
    semodule -i "$1/qdistro_broker.pp" 2>&1 \
        | grep -c "neverallow qdistro_broker_t"
}

build_pp() {  # $1 = build dir (already populated with sources)
    ( cd "$1" && make -f "$DEVEL/Makefile" clean >/dev/null 2>&1
      make -f "$DEVEL/Makefile" qdistro_broker.pp >/dev/null 2>&1 )
}

# Enable assertion checking — without this module neverallows are not checked.
if grep -q '^expand-check=' "$SEMANAGE_CONF" 2>/dev/null; then
    sed -i 's/^expand-check=.*/expand-check=1/' "$SEMANAGE_CONF"
else
    printf 'expand-check=1\n' >>"$SEMANAGE_CONF"
fi
grep -q '^expand-check=1' "$SEMANAGE_CONF" \
    && pass "semanage expand-check enabled (module neverallows now checked)" \
    || fail "could not enable expand-check"

# NEGATIVE CONTROL FIRST. Inject a forbidden raw-socket allow and confirm the
# neverallow rejects it. This proves assertion checking is actually LIVE and
# emits `neverallow qdistro_broker_t` lines on this system — without it, a clean
# 0-count below could mean "assertions off / semodule errored for some other
# reason", not "satisfiable". The clean verdict is gated on this control.
RATCHET_LIVE=0
mkdir -p "$WORK/bad"
cp "$SRC"/qdistro_broker.{te,if,fc} "$WORK/bad/" 2>/dev/null
printf '\nallow qdistro_broker_t self:rawip_socket create;\n' >>"$WORK/bad/qdistro_broker.te"
if build_pp "$WORK/bad" && [ -f "$WORK/bad/qdistro_broker.pp" ]; then
    BAD_FAILS=$(semodule -i "$WORK/bad/qdistro_broker.pp" 2>&1 \
        | grep -c "neverallow qdistro_broker_t self (rawip_socket")
    if [ "${BAD_FAILS:-0}" -ge 1 ]; then
        RATCHET_LIVE=1
        pass "build ratchet has teeth: forbidden rawip_socket allow rejected by neverallow"
    else
        fail "forbidden rawip_socket allow was NOT rejected — assertion checking is not live (ratchet toothless)"
    fi
else
    fail "could not build the forbidden module to test the ratchet"
fi

# Clean module: build, install, expect ZERO broker neverallow violations — but
# only TRUST a 0-count if the negative control above proved the mechanism live.
mkdir -p "$WORK/clean"
cp "$SRC"/qdistro_broker.{te,if,fc} "$WORK/clean/" 2>/dev/null
if build_pp "$WORK/clean" && [ -f "$WORK/clean/qdistro_broker.pp" ]; then
    pass "clean qdistro_broker.pp builds"
else
    fail "clean qdistro_broker.pp failed to build"
fi
if [ ! -f "$WORK/clean/qdistro_broker.pp" ]; then
    fail "clean module did not build — cannot judge neverallow-satisfiability"
elif [ "$RATCHET_LIVE" -ne 1 ]; then
    fail "skipping satisfiability verdict — negative control did not fire, so a 0-count is meaningless"
else
    CLEAN_FAILS=$(broker_neverallow_failures "$WORK/clean")
    if [ "${CLEAN_FAILS:-1}" -eq 0 ]; then
        pass "clean module is neverallow-satisfiable (0 qdistro_broker_t violations under expand-check=1, mechanism proven live)"
    else
        fail "clean module has $CLEAN_FAILS qdistro_broker_t neverallow violations — module is unsatisfiable"
    fi
fi

# restore trap reinstalls the clean module + expand-check=0.

# ---------------------------------------------------------------------------
echo "[s56b] $PASSCOUNT passes, $FAILCOUNT failures"
[ "$FAILCOUNT" -eq 0 ] && exit 0 || exit 1
