#!/bin/bash
# verify.sh — boot the built qdistro image in libvirt (qemu:///session,
# rootless), assert via SSH that the qdistro stack came up cleanly,
# capture screenshots and journal evidence to logs/verify/.
#
# Pattern mirrors qdistro/scripts/install/ui-agent-test.sh and the
# bats VM tests under qdistro/tests/integration/vm/.
#
# Load-bearing assertions are journal lines (per project memory:
# test_harness — "the load-bearing assertion is journal lines, not
# pixels"). Screenshots are evidence, not assertion fuel.
#
# Usage:
#   ./verify.sh                 # full lifecycle
#   ./verify.sh --keep          # leave the VM running for manual poking
#   ./verify.sh --teardown      # destroy + undefine + clean up

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Same default as build.sh/build-in-vm.sh: /tmp is a tmpfs on the build
# hosts, so a default-env build followed by a default-env verify must agree
# on /var/tmp or the artifact is simply not found (iso/14 Phase A item 6).
BUILD_DIR="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
STAMP="$(date +%y%m%d-%H%M)"
VERIFY_DIR="$HERE/logs/verify-${STAMP}$(date +%S)"
# AGENTS.md requires VM names end in YYMMDD-HHMM so parallel runs don't collide.
VM="${QDISTRO_VERIFY_VM:-qdistro-verify-${STAMP}}"
SSH_PORT="${QDISTRO_VERIFY_PORT:-2299}"
SSH_USER="admin"
SSH_PASS="${QDISTRO_IMAGE_PASSWORD:-qdistro}"
URI="qemu:///session"

log()  { printf '\033[1;36m[verify]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[verify] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[verify] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

teardown() {
    log "tearing down VM $VM"
    virsh -c "$URI" destroy   "$VM" 2>/dev/null || true
    virsh -c "$URI" undefine  "$VM" --nvram 2>/dev/null || true
    rm -f "$BUILD_DIR/$VM.qcow2"
}

case "${1:-}" in
  --teardown) teardown; exit 0 ;;
esac

#-- 0. Locate the built image -------------------------------------------------
# QDISTRO_IMAGE names the artifact explicitly (the CI gate passes the raw it
# just inspected statically, so both stages judge the SAME file). Without
# it exactly one candidate may exist: picking "the first find hit" among a
# stale qcow2 and a fresh raw booted an arbitrary artifact (Phase B review).
if [ -n "${QDISTRO_IMAGE:-}" ]; then
    IMG="$QDISTRO_IMAGE"
    [ -f "$IMG" ] || die "QDISTRO_IMAGE does not exist: $IMG"
else
    mapfile -t _imgs < <(find "$BUILD_DIR" -maxdepth 2 \( -name '*.raw' -o -name '*.qcow2' \) 2>/dev/null | grep -v -F "$VM" | grep -v -- '-verify-' | sort)
    case "${#_imgs[@]}" in
        1) IMG="${_imgs[0]}" ;;
        0) die "no image in $BUILD_DIR; run build.sh first" ;;
        *) die "${#_imgs[@]} images under $BUILD_DIR (${_imgs[*]}); set QDISTRO_IMAGE to the one to boot" ;;
    esac
fi
log "image: $IMG"

mkdir -p "$VERIFY_DIR/screenshots" "$VERIFY_DIR/journal"

#-- 1. Make a qcow2 overlay so we don't trash the master ----------------------
# teardown() must happen BEFORE we create the overlay, otherwise it
# deletes the file we just made.
teardown
OVERLAY="$BUILD_DIR/$VM.qcow2"
case "$IMG" in
  *.raw)   FMT=raw   ;;
  *.qcow2) FMT=qcow2 ;;
  *) die "unknown image format: $IMG" ;;
esac
qemu-img create -F "$FMT" -b "$IMG" -f qcow2 "$OVERLAY" >/dev/null
log "overlay: $OVERLAY (backing $IMG)"

#-- 2. Locate UEFI firmware (qemu:///session needs an absolute path) ----------
for c in /usr/share/qemu/ovmf-x86_64-4m.bin \
         /usr/share/qemu/ovmf-x86_64-code.bin \
         /usr/share/qemu/ovmf-x86_64.bin \
         /usr/share/OVMF/OVMF_CODE.fd ; do
    [ -f "$c" ] && OVMF="$c" && break
done
[ -n "${OVMF:-}" ] || die "no OVMF firmware (install qemu-ovmf-x86_64 or OVMF)"
log "uefi firmware: $OVMF"

#-- 3. Domain XML (rootless qemu:///session, SSH port forward 2299->22) ------
NVRAM="$VERIFY_DIR/$VM.nvram.fd"
# Seed nvram from the matching template if present so secure-boot vars
# don't trip kiwi's UEFI bootloader.
for tpl in /usr/share/qemu/ovmf-x86_64-vars.bin /usr/share/qemu/ovmf-x86_64-4m-vars.bin /usr/share/OVMF/OVMF_VARS.fd; do
    [ -f "$tpl" ] && cp "$tpl" "$NVRAM" && break
done
DOMAIN_XML=$(cat <<XML
<domain type='kvm'>
  <name>$VM</name>
  <memory unit='MiB'>4096</memory>
  <vcpu>2</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>$OVMF</loader>
    <nvram template='$OVMF'>$NVRAM</nvram>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$OVERLAY'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='user'>
      <backend type='passt'/>
      <model type='virtio'/>
      <portForward proto='tcp' address='127.0.0.1'>
        <range start='$SSH_PORT' to='22'/>
      </portForward>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <video><model type='virtio' heads='1'/></video>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
  </devices>
</domain>
XML
)

#-- 4. Define + start ---------------------------------------------------------
log "defining $VM"
echo "$DOMAIN_XML" | virsh -c "$URI" define /dev/stdin
log "starting $VM"
virsh -c "$URI" start "$VM"

#-- 5. Screenshot helper ------------------------------------------------------
shoot() {
    local label="$1"
    local out="$VERIFY_DIR/screenshots/$label.png"
    local tmp
    tmp=$(mktemp --suffix=.ppm)
    if virsh -c "$URI" screenshot "$VM" "$tmp" >/dev/null 2>&1; then
        if command -v convert >/dev/null 2>&1; then
            convert "$tmp" "$out" 2>/dev/null && log "screenshot: $out"
        elif command -v ffmpeg >/dev/null 2>&1; then
            ffmpeg -y -loglevel error -i "$tmp" "$out" && log "screenshot: $out"
        fi
        rm -f "$tmp"
    else
        warn "screenshot failed (display not ready yet)"
    fi
}

shoot 00-just-booted

#-- 5b. Start sshd via the guest agent (sshd is NOT enabled by default) -------
# The shipped image intentionally does not enable sshd at boot (avoids a
# network-reachable default-credential exposure; see image/config.sh). The
# qemu-guest-agent provides an out-of-band virtio-serial channel we use to
# start sshd on demand for these verification assertions — no network path is
# baked into the image. guest-exec runs as root inside the guest.
qga() {
    virsh -c "$URI" qemu-agent-command "$VM" "$1" 2>/dev/null
}
# qga_root <shell> — run <shell> as ROOT in the guest through the agent
# (guest-exec + guest-exec-status), print its stdout, relay its stderr, and
# return its exit code. This is the verifier's root channel: it works on every
# profile (the release image deletes admin's sudoers rule), so an assertion
# that needs root reads through here, not through `sudo -n` over SSH.
# The status polling has a 60 s deadline (each agent call is bounded by
# libvirt, not by this shell); a timeout or an agent error returns 97/98
# (never 0).
command -v jq >/dev/null 2>&1 || die "jq not installed; install with: sudo zypper in jq"
qga_root() {
    local cmd="$1" out pid st="" deadline
    out=$(qga "$(jq -cn --arg c "$cmd" '{execute:"guest-exec",arguments:{path:"/bin/bash",arg:["-c",$c],"capture-output":true}}')")
    pid=$(printf '%s' "$out" | jq -r '.return.pid // empty' 2>/dev/null)
    [ -n "$pid" ] || { echo "qga_root: guest-exec failed: $out" >&2; return 97; }
    deadline=$(( $(date +%s) + 60 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        st=$(qga "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}")
        [ "$(printf '%s' "$st" | jq -r '.return.exited' 2>/dev/null)" = true ] && break
        sleep 1
    done
    [ "$(printf '%s' "$st" | jq -r '.return.exited' 2>/dev/null)" = true ] \
        || { echo "qga_root: timed out after 60s: $cmd" >&2; return 98; }
    printf '%s' "$st" | jq -r '.return."out-data" // empty' | base64 -d
    printf '%s' "$st" | jq -r '.return."err-data" // empty' | base64 -d >&2
    return "$(printf '%s' "$st" | jq -r '.return.exitcode // 99')"
}
log "waiting for qemu-guest-agent (max 180s)..."
qga_deadline=$(( $(date +%s) + 180 ))
qga_up=0
while [ "$(date +%s)" -lt "$qga_deadline" ]; do
    if qga '{"execute":"guest-ping"}' | grep -q '"return"'; then qga_up=1; break; fi
    sleep 3
done
[ "$qga_up" = 1 ] || { shoot 99-qga-timeout; die "guest agent never responded; cannot start sshd (see $VERIFY_DIR/screenshots/99-qga-timeout.png)"; }
log "guest agent up; starting sshd over the agent channel"
exec_out=$(qga '{"execute":"guest-exec","arguments":{"path":"/usr/bin/systemctl","arg":["start","sshd.service"],"capture-output":true}}')
qga_pid=$(printf '%s' "$exec_out" | grep -oE '"pid":[0-9]+' | grep -oE '[0-9]+' | head -1)
[ -n "$qga_pid" ] || { shoot 99-qga-exec; die "guest-exec to start sshd failed: $exec_out"; }
# Poll guest-exec-status until the systemctl call exits (bounded).
st_deadline=$(( $(date +%s) + 60 ))
while [ "$(date +%s)" -lt "$st_deadline" ]; do
    st=$(qga "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$qga_pid}}")
    printf '%s' "$st" | grep -q '"exited":true' && break
    sleep 2
done
ec=$(printf '%s' "$st" | grep -oE '"exitcode":[0-9]+' | grep -oE '[0-9]+' | head -1)
[ "${ec:-0}" = 0 ] || warn "systemctl start sshd returned exitcode=$ec (continuing; SSH wait loop will confirm)"

#-- 6. SSH wrapper + wait for auth to actually succeed -----------------------
# First-boot resize + greetd autologin together take ~2-5 min; we poll
# real SSH auth (not /dev/tcp — passt-forwarded ports accept connections
# even when sshd isn't listening yet) until it returns 0.
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o LogLevel=ERROR"
command -v sshpass >/dev/null 2>&1 || die "sshpass not installed; install with: sudo zypper in sshpass"
remote() {
    sshpass -p "$SSH_PASS" ssh $SSH_OPTS -p "$SSH_PORT" "$SSH_USER@127.0.0.1" "$@"
}

log "waiting for sshd to accept auth (max 600s)..."
deadline=$(( $(date +%s) + 600 ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if remote 'true' 2>/dev/null; then
        ready=1; break
    fi
    sleep 5
done
[ "$ready" = 1 ] || { shoot 99-ssh-timeout; die "SSH never came up; see $VERIFY_DIR/screenshots/99-ssh-timeout.png"; }
shoot 01-ssh-ready

# admin's user systemd manager comes up under linger right after boot and
# reaches default.target, which trails the moment sshd accepts auth by ~5-10s
# and can flap under load. The assertions below query admin's user manager
# (systemctl --user cat/show/is-active for qdwin-session.target, qdlocker, ...),
# so they need that manager up and settled. Instead of a fixed empirical 30s
# sleep, poll `systemctl --user is-system-running` until the manager reports it
# has finished bringing up default.target. Bounded: if it never settles within
# the budget we fall through and let the assertions below report the real
# failure rather than hanging here.
#
# `is-system-running` prints (and exits 0 for) `running`; during bring-up it
# prints `initializing`/`starting`, and on a unit failure it prints `degraded`
# (exit 1). We accept `running` OR `degraded` as "settled" — a degraded manager
# has still finished its startup transaction, and the individual assertions
# below are what should adjudicate any unit failure, not this gate.
# The SYSTEM manager first: sshd is started through the agent within a few
# seconds of boot now (run 28 onward), so the assertions can otherwise run
# while Type=notify units are still activating -- run 29 sampled the admin
# broker four seconds before it finished starting and failed two rows that
# run 28 had passed. Bounded; running or degraded both mean "startup done".
sys_wait_deadline=$(( $(date +%s) + 180 ))
log "waiting for the system manager to finish startup (max 180s)..."
sys_state=""
while [ "$(date +%s)" -lt "$sys_wait_deadline" ]; do
    sys_state="$(remote 'systemctl is-system-running' 2>/dev/null || true)"
    case "$sys_state" in
        running|degraded) log "system manager settled (is-system-running=$sys_state)"; break ;;
    esac
    sleep 3
done
case "$sys_state" in
    running|degraded) ;;
    *) warn "system manager did not settle within 180s (is-system-running='$sys_state'); running assertions anyway" ;;
esac

user_mgr_check() {
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-system-running" 2>/dev/null
}
mgr_wait_deadline=$(( $(date +%s) + 120 ))
log "waiting for admin's user manager to settle (max 120s)..."
mgr_state=""
while [ "$(date +%s)" -lt "$mgr_wait_deadline" ]; do
    mgr_state="$(user_mgr_check || true)"
    case "$mgr_state" in
        running|degraded)
            log "admin user manager settled (is-system-running=$mgr_state)"
            break
            ;;
    esac
    sleep 3
done
case "$mgr_state" in
    running|degraded) ;;
    *) warn "admin user manager did not settle within 120s (is-system-running='$mgr_state'); running assertions anyway" ;;
esac

#-- 8. Assertions (journal-side, per project memory) --------------------------
PASS=0; FAIL=0
expect() {
    local label="$1"; shift
    if "$@" >>"$VERIFY_DIR/assert.log" 2>&1; then
        echo "PASS: $label" | tee -a "$VERIFY_DIR/report.txt"
        PASS=$((PASS+1))
    else
        echo "FAIL: $label" | tee -a "$VERIFY_DIR/report.txt"
        FAIL=$((FAIL+1))
    fi
}

log "running in-VM assertions..."

# Basic boot
expect "ssh reachable as admin"      remote 'id -un | grep -qx admin'
expect "/etc/os-release is qdistro"  remote "grep -Eq '^ID=\"?qdistro\"?$' /etc/os-release"
expect "hostname is qdistro"         remote "grep -qx qdistro /etc/hostname"

# Greetd brought up the graphical target
expect "greetd.service active"       remote 'systemctl is-active greetd.service'
expect "default target is graphical" remote 'systemctl get-default | grep -qx graphical.target'

# qdistro admin broker on the system bus
expect "qdistro-admin-broker active" remote 'systemctl is-active qdistro-admin-broker.service || sudo -n systemctl is-active qdistro-admin-broker.service'
expect "broker owns dbus name"       remote "sudo -n busctl list --no-pager | grep -q org.qdistro.AdminBroker1"

# Greeter boot path: greetd execs /usr/bin/qdgreeter (greetd-config.toml),
# which after auth runs qdwin-session-launcher -> `systemctl --user start
# qdwin-session.target`. Assert the exact binary + units that path needs.
expect "qdgreeter binary present" \
    remote 'test -x /usr/bin/qdgreeter'
expect "greetd execs an existing greeter (no exec failure in journal)" \
    remote 'sudo -n journalctl -u greetd -b --no-pager | grep -Eiq "No such file|exec.*qdgreeter.*fail|failed to execute" && exit 1 || exit 0'
expect "qdwin-session.target user unit installed" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user cat qdwin-session.target >/dev/null 2>&1"
expect "qdshell wanted by qdwin-session.target" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p Wants qdwin-session.target | grep -q qdshell.service"
expect "qdlocker wanted by qdwin-session.target" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p Wants qdwin-session.target | grep -q qdlocker.service"

# qdlocker.service ExecStart must point at the binary the image actually
# installed (/usr/bin/qdlocker from the --prefix=/usr pip install). If the
# unit still carries the upstream /usr/local/bin/qdlocker ExecStart, the
# locker 203/EXECs at boot and never starts — wiring it into the session
# Wants= is then a no-op (finding #16, BROKEN remediation). These are GATING
# assertions: the unit must load with a resolvable ExecStart AND not show an
# exec failure in its journal. The Wants= check above alone could not catch
# an ExecStart/binary-path mismatch.
expect "qdlocker.service ExecStart resolves to an installed binary" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p ExecStart qdlocker.service | grep -oE 'path=[^ ;]+' | head -n1 | cut -d= -f2 | xargs test -x"
expect "qdlocker.service is loaded (not error/masked/not-found)" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show -p LoadState qdlocker.service | grep -qx 'LoadState=loaded'"
expect "qdlocker.service did not 203/EXEC (ExecStart/binary-path match)" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u qdlocker.service -b --no-pager 2>/dev/null | grep -Eiq '203/EXEC|No such file or directory|Failed to locate executable|Failed at step EXEC' && exit 1 || exit 0"
# qdlocker.service is Type=simple, so it should reach `active` promptly. We
# require `active` (NOT merely `activating`) — a unit stuck activating or
# flapping under Restart=always is a real failure, not something to tolerate.
# A short bounded settle absorbs only first-boot bring-up timing; the permanent
# ExecStart-failure case is already caught definitively by the 203/EXEC journal
# check above.
#
# Crash-loop hardening: reaching `active` once is not enough — a unit that
# becomes active briefly and then exits under Restart=always could be sampled
# during an active window. So after first reaching active we settle, then
# require ActiveState=active AND SubState=running AND a low restart count
# (NRestarts<=1) so a flapping locker fails the gate.
#
# qdlocker is WantedBy qdwin-session.target, which only a real greeter
# authentication starts (see the VT-escape note below): on the shipped
# image this verifier never logs in through qdgreeter, so with the greeter
# on tty3 the locker is legitimately inactive/dead with NRestarts=0. The
# row therefore adjudicates by session state: session up -> must hold
# active/running; session exactly inactive -> locker must be loaded,
# inactive/dead, never restarted (a locker that started and died without
# a session IS flapping; a missing unit would otherwise look identical
# to "never started"; a failed/activating session is not the no-login
# state and fails this row). Until run 29 this row demanded `active`
# unconditionally, which no password-gated image can satisfy; it had
# never passed.
expect "qdlocker.service is healthy: holds active/running with a session, inactive and never failed without one" \
    remote "sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 sh -c '
        sess=\$(systemctl --user is-active qdwin-session.target)
        if [ \"\$sess\" = active ]; then
            for i in 1 2 3 4 5 6 7 8 9 10; do
                [ \"\$(systemctl --user is-active qdlocker.service)\" = active ] && break
                sleep 1
            done
            sleep 3
        fi
        read load as ss nr <<EOF2
\$(systemctl --user show -p LoadState -p ActiveState -p SubState -p NRestarts --value qdlocker.service | tr \"\\n\" \" \")
EOF2
        echo \"qdwin-session.target=\$sess qdlocker LoadState=\$load ActiveState=\$as SubState=\$ss NRestarts=\$nr\"
        if [ \"\$sess\" = active ]; then
            [ \"\$as\" = active ] && [ \"\$ss\" = running ] && [ \"\${nr:-99}\" -le 1 ]
        elif [ \"\$sess\" = inactive ]; then
            [ \"\$load\" = loaded ] && [ \"\$as\" = inactive ] && [ \"\$ss\" = dead ] && [ \"\${nr:-99}\" -eq 0 ]
        else
            false
        fi'"

# NOT asserted here: the locked-session VT escape
# (tests/integration/vm/probes/vt-escape-lockdown.sh). Two reasons, both
# specific to this verifier, and both worth writing down so nobody adds it
# back without fixing them first:
#
#  1. It needs root. This verifier's `remote` is an SSH login as admin, and
#     a release-profile build deliberately deletes /etc/sudoers.d/99-admin
#     (image/config.sh) — so `sudo -n` cannot work there. Root IS available
#     profile-independently through the guest agent (qga_root above; the
#     Phase D rows use it), and the older `sudo -n` rows in this file should
#     migrate to it rather than be copied. Weakening the shipped image's sudo
#     policy to suit the verifier is not an option.
#  2. It would measure the wrong process. The shipped greetd config has only
#     a `_greeter` default_session and its initial_session is commented out,
#     and image/config.sh removes qdwin-session's default-target enablement
#     so only a real greeter authentication starts it. This verifier logs in
#     over SSH and never authenticates through qdgreeter, so tty3 belongs to
#     qdgreeter, not the post-auth qdwin session whose LOCKED screen the
#     probe exists to protect. A green result here would be green for the
#     wrong thing.
#
# The runtime probe therefore lives in tests/integration/vm/vt-escape-lockdown.bats,
# which converts its own disposable VM (enable-qdgreeter.sh + a test-only
# autologin) and so does reach a real qdwin session on tty3 — and which runs
# in the bats lane that `qci full` actually invokes.

expect "weston (qdwin) on disk" \
    remote 'test -f /usr/lib64/weston/qdwin-shell.so || test -f /usr/lib/weston/qdwin-shell.so'

# One chain (todo/iso/14 Phase D): the image ran the bootstrap's installer
# chain, so the isolation ladder above tier 2 is on the stick. Tier 3 is the
# top of the SUPPORTED ladder (spawn helper + group + polkit action); the
# tier-4 host control script is what spawn-tier4.sh falls back to on an
# installed image (experimental tier, host launch code only).
# The spawn/cleanup helpers are symlinks into /root/qdistro-src (mode 0700:
# admin cannot resolve them, root can; the helper runs as root via polkit).
# So: the link and its target's name from admin's view, the target's
# executability from root's.
expect "tier-3 spawn helper installed (chain step tier3)" \
    remote 'test -L /usr/local/bin/qdistro-tier3-spawn && [ "$(readlink /usr/local/bin/qdistro-tier3-spawn)" = /root/qdistro-src/qdistro/tier3/spawn-tier3.sh ] && getent group qdistro-tier3 >/dev/null && test -f /usr/share/polkit-1/actions/org.qdistro.tier3.policy'
expect "tier-3 spawn/cleanup helper targets executable (root view)" \
    qga_root 'test -x /root/qdistro-src/qdistro/tier3/spawn-tier3.sh && test -x /root/qdistro-src/qdistro/tier3/qdistro-tier3-cleanup.sh && test -x /usr/local/bin/qdistro-tier3-spawn'
# passwd -S needs root; an empty or unexpected status line is a FAIL (the
# case pattern matches the second field exactly, so silence cannot pass).
expect "tier-3 silo users exist with locked passwords" \
    qga_root 'for u in user1 user2; do id -u "$u" >/dev/null || exit 1; s=$(passwd -S "$u") || exit 1; case "$s" in "$u L "*|"$u LK "*) ;; *) echo "not locked: ${s:-<no output>}"; exit 1;; esac; done'
expect "tier-3 runtime dir created at boot by tmpfiles" \
    remote 'test -d /run/qdistro-tier3'
expect "tier-4 host control script installed (chain step tier4-host)" \
    remote 'test -f /usr/share/qdistro/tier4-vm/tier4_control.py && test -f /usr/share/qdistro/tier4-vm/tier4_chrome.py'
expect "sdk (qdistro_app) importable" \
    remote 'python3 -c "import qdistro_app"'
# The DONE bar, on the booted image: the steps recorded as installed equal
# the bootstrap's chain for this image's profile (dev-only steps excluded
# outside dev). chain_expected_names is the bootstrap's own definition, read
# from the on-image source tree -- under /root, hence the root channel.
expect "installer chain record equals the bootstrap chain for this profile" \
    qga_root 'p=$(sed -n "s/^PROFILE=//p" /etc/qdistro/release); [ -n "$p" ] || { echo "no PROFILE in /etc/qdistro/release"; exit 1; };
            exp=$(QDISTRO_PROFILE="$p" bash -c ". /root/qdistro-src/qdistro/scripts/install/qdistro-bootstrap.sh; resolve_profile >/dev/null; chain_expected_names") || { echo "chain_expected_names failed"; exit 1; };
            rec=$(grep -vE "^[[:space:]]*(#|$)" /var/lib/qdistro/bootstrap/installer-chain.state);
            [ -n "$exp" ] && [ "$exp" = "$rec" ] && { echo "chain ($p): $(echo $exp)"; exit 0; };
            echo "expected: $(echo $exp)"; echo "recorded: $(echo $rec)"; exit 1'
expect "no media/multimachine/recall artefacts (not in the chain)" \
    remote 'for f in /etc/systemd/system/qdistro-media-exec.socket /usr/local/bin/qdistro-mm-broker /usr/local/bin/qdistro-recall; do test -e "$f" && exit 1; done; exit 0'
expect "qdshell QML installed"  remote 'test -d /usr/share/quickshell/qdshell'

# Priority 0/1 journal entries. The single benign one we tolerate is the
# kernel's "RDSEED32 is broken" CPUID-quirk notice on qemu hosts.
expect "no unexpected priority=0/1 errors in journal" \
    remote 'sudo -n journalctl -b -p emerg..alert --no-pager -q | grep -v "RDSEED32 is broken" | grep -q . && exit 1 || exit 0'

#-- 9. Capture journals + systemctl state for evidence -----------------------
log "capturing journals + systemd state"
remote 'sudo -n journalctl -b --no-pager'                > "$VERIFY_DIR/journal/full.log"      2>&1 || true
remote 'sudo -n journalctl -b -p err --no-pager'         > "$VERIFY_DIR/journal/errors.log"    2>&1 || true
remote 'sudo -n journalctl -u qdistro-admin-broker --no-pager' > "$VERIFY_DIR/journal/broker.log" 2>&1 || true
remote 'sudo -n journalctl -u greetd --no-pager'         > "$VERIFY_DIR/journal/greetd.log"    2>&1 || true
remote 'systemctl --failed --no-pager'                   > "$VERIFY_DIR/journal/failed-units.log" 2>&1 || true
remote 'cat /var/lib/qdistro/bootstrap/installer-chain.state' > "$VERIFY_DIR/journal/installer-chain.state" 2>&1 || true
remote 'cat /etc/qdistro/release'                        > "$VERIFY_DIR/journal/release.txt"       2>&1 || true
remote 'sudo -n -u admin XDG_RUNTIME_DIR=/run/user/1000 systemctl --user --no-pager status qdwin-session.target qdwin-compositor.service qdshell.service qdlocker.service' \
    > "$VERIFY_DIR/journal/user-units.log" 2>&1 || true

shoot 02-fully-booted
sleep 30
shoot 03-after-30s
sleep 30
shoot 04-after-60s

#-- 11. Report -----------------------------------------------------------------
TOTAL=$((PASS+FAIL))
{
    echo ""
    echo "=========================================="
    echo " qdistro verify summary"
    echo " image:   $IMG"
    echo " when:    $(date -Is)"
    echo " pass:    $PASS / $TOTAL"
    echo " fail:    $FAIL / $TOTAL"
    echo " artifacts: $VERIFY_DIR"
    echo "=========================================="
} | tee -a "$VERIFY_DIR/report.txt"

if [ "${1:-}" = --keep ]; then
    log "VM left running (--keep). To tear down: $0 --teardown"
else
    teardown
fi

[ "$FAIL" -eq 0 ]
