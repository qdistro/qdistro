#!/bin/bash
# tier3s/spike/lanes-c2.sh — GUEST driver (root) for the Phase C2 lane dev-run.
# The C2 dev VM already carries the tier3s stack, so this is NOT the bats
# lane's fresh-worker setup: it reinstalls the tested commit's stack (broker
# + templates + session manager with QDISTRO_TIER3S=1), rebuilds the
# headless-smoke OCI archive into /var/tmp/t3s-img (so the image carries the
# tested commit's headless-smoke.sh), stages the image in admin's store, and
# then runs each named tests/integration/vm/s1* driver exactly as the qci
# lane does (the driver fetches nothing else: the lib sits beside it).
#
#   lanes-c2.sh <src tree> <driver.sh> [<driver.sh> ...]
set -u
SRC=$1; shift
DL=/var/tmp/t3s-dl
cd "$DL" || { echo "no $DL"; exit 2; }
P=0; F=0
is() { # is <name> <got> <want>
    if [ "$2" = "$3" ]; then echo "PASS: $1"; P=$((P+1))
    else echo "FAIL: $1 (want '$3', got '$2')"; F=$((F+1)); fi
}
step() { echo; echo "=== $* ==="; }

step "0. reinstall the tested stack"
info_out=$(bash "$SRC/scripts/install/install-broker-for-qdwin.sh" "$SRC/broker" 2>&1); is "install-broker rc" "$?" 0
info_out=$(bash "$SRC/scripts/install/install-templates-for-vm.sh" "$SRC" 2>&1); is "install-templates rc" "$?" 0
info_out=$(QDISTRO_TIER3S=1 bash "$SRC/scripts/install/install-session-manager.sh" "$SRC/session_manager" 2>&1); is "install-session-manager rc" "$?" 0
printf '%s\n' "$info_out" | tail -3 | sed 's/^/    /'
systemd-tmpfiles --create /usr/lib/tmpfiles.d/qdistro-tier3s.conf 2>/dev/null || true

step "0b. reset tier3s state (the dev VM is reused; a stale launch silo row
self-heals back to life on every manager restart)"
# every tier3s silo row: stop then delete (a row left Active relaunches)
runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 \
    busctl --system --timeout=300 --json=short call org.qdistro.SessionManager1 \
    /org/qdistro/SessionManager1 org.qdistro.SessionManager1 ListSilos 2>/dev/null | python3 -c '
import json, sys, subprocess
try: rows = json.loads(json.load(sys.stdin)["data"][0])
except Exception: sys.exit(0)
for s in rows:
    if s.get("kind") != "tier3s": continue
    for m, sig, a in (("StopSilo","si",10), ("DeleteSilo","s",s["name"])):
        subprocess.run(["runuser","-u","admin","--","env","-i","PATH=/usr/bin:/bin",
            "HOME=/home/admin","XDG_RUNTIME_DIR=/run/user/1000","busctl","--system","--timeout=300",
            "call","org.qdistro.SessionManager1","/org/qdistro/SessionManager1",
            "org.qdistro.SessionManager1",m,sig] + ([s["name"],str(a)] if sig=="si" else [s["name"]]),
            capture_output=True)
' || :
# strays: launch units, scopes, stanzas, records — the designed sweeps first
for u in $(systemctl list-units --all --plain --no-legend 'qdistro-tier3s-silo@*.service' 'qdistro-tier3s-*.scope' | awk '{print $1}'); do
    systemctl stop "$u" 2>/dev/null || :
done
/usr/libexec/qdistro/qdistro-tier3s-cleanup --reap-stale >/dev/null 2>&1 || :
rm -f /run/qdistro/tier3s-launch/*.env 2>/dev/null || :
# test-authored broker rules: the drivers own 50-tier3s-qci.yaml; spike
# sessions may have left others (e.g. 50-t3s-probe.yaml) — any leftover
# tier3s rule defeats set_rule none's "unknown" precondition
rm -f /etc/qdistro/rules.d/50-tier3s-qci.yaml /etc/qdistro/rules.d/50-t3s-probe.yaml
sleep 2
left=$(systemctl list-units --all --plain --no-legend 'qdistro-tier3s-*.scope' | grep -c .)
is "reset: no tier3s scope left" "$left" 0
is "reset: no tier3s rule files left" "$(grep -lE 't3s|tier3s' /etc/qdistro/rules.d/* 2>/dev/null | wc -l)" 0
is "reset: no control records left" "$(find /run/qdistro-tier3s-ctl -mindepth 1 -maxdepth 1 -regextype egrep -regex '.*/[0-9a-f]{32}' 2>/dev/null | wc -l)" 0

# guest-setup's step 6: drivers that don't set rules themselves (s120,
# s130, s131) expect the smoke-spawn allow rule the real lane installs
cat > /etc/qdistro/rules.d/50-tier3s-qci.yaml <<'YAML'
# test-authored by the tier 3s qci drivers (tests/integration/vm/tier3s-*)
- name: tier3s-qci-allow
  decision: allow
  match:
    uid: 1000
    action: "qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke"
YAML
chmod 0644 /etc/qdistro/rules.d/50-tier3s-qci.yaml
# the broker reloads rules asynchronously — poll before asserting
rule_seen() {
    [ "$(runuser -u admin -- busctl --system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1 CheckPermission 'sa{sv}' \
        'qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke' 0 2>/dev/null)" = 's "allow"' ]
}
n=0; until rule_seen || [ $((n+=1)) -ge 80 ]; do sleep 0.25; done
is "reset: smoke-spawn allow rule installed" "$(rule_seen && echo yes || echo no)" yes

step "1. rebuild the headless-smoke image (tested commit's headless-smoke.sh)"
b=/var/tmp/t3s-build; rm -rf "$b"
install -d -m 0755 "$b" && cp -a "$SRC/tier3s" "$b/" && cp "$SRC/snapshot.conf" "$b/"
d=/var/tmp/t3s-img; rm -rf "$d"; install -d -o admin -m 0755 "$d"
out=$(runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin LOGNAME=admin \
    XDG_RUNTIME_DIR=/run/user/1000 bash "$b/tier3s/make-tier3s-image.sh" --oci-archive "$d" headless-smoke 2>&1); rc=$?
printf '%s\n' "$out" | tail -6 | sed 's/^/    /'
is "make-tier3s-image rc" "$rc" 0
# guest-setup's archive layout: headless-smoke lands at image.oci.tar (the
# name tier3s-guest-lib's ensure_silo_image looks up); other workloads keep
# tier3s-<w>.oci.tar.
[ -f "$d/tier3s-headless-smoke.oci.tar" ] && ln -f "$d/tier3s-headless-smoke.oci.tar" "$d/image.oci.tar" \
    && chmod 0644 "$d/image.oci.tar" || is "oci archive staged" missing present

step "2. drivers"
for drv in "$@"; do
    echo "--- $drv ---"
    bash "$DL/$drv"; rc=$?
    echo "--- $drv rc=$rc ---"
    [ "$rc" -eq 0 ] || F=$((F+1))
done
echo "[lanes-c2] $P passes, $F failures (+ per-driver summaries above)"
exit $F
