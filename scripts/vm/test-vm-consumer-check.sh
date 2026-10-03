#!/bin/bash
# test-vm-consumer-check.sh — boot the shipped test VM the way a consumer
# would and check that it is usable for further testing. Run on the CI
# runner by .github/workflows/qdistro-test-vm.yml after the image is final.
#
#   test-vm-consumer-check.sh VM_DIR VM_IMAGE QEMU_ACCEL SNAPSHOT
#
# The image itself is never written: the guest boots a throwaway qcow2
# overlay with fresh UEFI variables and its own cloud-init seed (new
# instance-id, new SSH key), as on a tester's machine. Checks:
#   access    the consumer's key logs in; the build key and password SSH do
#             not; the QEMU guest agent answers and runs commands (guest-exec,
#             the qci exec path)
#   identity  machine-id regenerated; SSH host keys regenerated (warning)
#   health    systemd reaches "running" with no failed unit; qdistro
#             services and bus names are up; the installer chain is complete
#   packages  repositories are only the pinned history snapshot, and zypper
#             can still resolve test packages from it
#   desktop   greetd shows qdgreeter on the virtual display; typing the
#             password on QEMU's virtual keyboard starts qdwin, qdshell and
#             qdlocker. Screenshots: consumer-greeter.png, consumer-desktop.png
set -euo pipefail

VM_DIR=${1:?VM_DIR}
VM_IMAGE=${2:?VM_IMAGE}
ACCEL=${3:?QEMU_ACCEL}
SNAPSHOT=${4:?SNAPSHOT}
PASSWORD=${QDISTRO_TEST_VM_PASSWORD:-qdistro}
C=$VM_DIR/consumer
PORT=${QDISTRO_CONSUMER_SSH_PORT:-2223}
# Ubuntu runner paths; override for a local run.
OVMF_CODE=${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}
OVMF_VARS=${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}

exec > >(tee "$VM_DIR/consumer-check.log") 2>&1

log() { echo "[consumer] $*"; }
fail() { echo "[consumer] FAIL: $*"; exit 1; }

rm -rf "$C"
mkdir -p "$C"
# UNIX socket paths must stay under 108 bytes.
SOCK=$(mktemp -d /tmp/qdvm.XXXXXX)
qemu-img create -q -f qcow2 -b "$VM_IMAGE" -F qcow2 "$C/disk.qcow2"
ssh-keygen -q -t ed25519 -N '' -C consumer -f "$C/key"
# The documented consumer seed: only a key. The image makes admin
# cloud-init's default user without locking its password; sudo comes from the
# image (dev profile), not from this seed.
printf '#cloud-config\nssh_authorized_keys:\n  - %s\n' "$(cat "$C/key.pub")" > "$C/user-data"
printf 'instance-id: qdistro-consumer-1\nlocal-hostname: qdistro-consumer\n' > "$C/meta-data"
cloud-localds "$C/seed.img" "$C/user-data" "$C/meta-data"
cp "$OVMF_VARS" "$C/OVMF_VARS.fd"

qemu-system-x86_64 -machine "q35,accel=$ACCEL" -cpu max -m 6144 -smp "$(nproc)" \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$C/OVMF_VARS.fd" \
    -drive if=virtio,format=qcow2,file="$C/disk.qcow2" \
    -drive if=virtio,format=raw,readonly=on,file="$C/seed.img" \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:$PORT-:22 \
    -device virtio-net-pci,netdev=net0 \
    -device virtio-vga \
    -device virtio-serial-pci \
    -chardev socket,id=qga0,path="$SOCK/qga",server=on,wait=off \
    -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0 \
    -qmp unix:"$SOCK/qmp",server=on,wait=off \
    -display none -serial file:"$VM_DIR/serial-consumer.log" -monitor none \
    > "$VM_DIR/qemu-consumer.log" 2>&1 &
qemu_pid=$!
trap 'kill "$qemu_pid" 2>/dev/null || true; rm -rf "$C/disk.qcow2" "$SOCK"' EXIT

# One QMP (or guest-agent) command per connection; prints the reply.
cat > "$C/qmp.py" <<'PY'
import json, socket, sys
path, command = sys.argv[1], json.loads(sys.argv[2])
s = socket.socket(socket.AF_UNIX)
s.settimeout(float(sys.argv[3]) if len(sys.argv) > 3 else 30)
s.connect(path)
f = s.makefile("rw")
def call(c):
    f.write(json.dumps(c) + "\n"); f.flush()
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r:
            return r
if path.endswith("/qmp"):
    json.loads(f.readline())          # greeting
    call({"execute": "qmp_capabilities"})
r = call(command)
print(json.dumps(r))
sys.exit(1 if "error" in r else 0)
PY
qmp() { python3 "$C/qmp.py" "$SOCK/qmp" "$1" >/dev/null; }

# screendump writes PPM; convert to PNG and report "<distinct colours> <pixel sha>".
cat > "$C/shot.py" <<'PY'
import hashlib, struct, sys, zlib
src, dst = sys.argv[1], sys.argv[2]
data = open(src, "rb").read()
fields, pos = [], 0
while len(fields) < 4:
    while data[pos:pos+1].isspace(): pos += 1
    if data[pos:pos+1] == b"#":
        pos = data.index(b"\n", pos); continue
    end = pos
    while not data[end:end+1].isspace(): end += 1
    fields.append(data[pos:end]); pos = end
pos += 1
assert fields[0] == b"P6", fields[0]
w, h = int(fields[1]), int(fields[2])
pix = data[pos:pos + w * h * 3]
rows = b"".join(b"\0" + pix[y*w*3:(y+1)*w*3] for y in range(h))
def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
open(dst, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                      + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))
colours = {pix[i:i+3] for i in range(0, len(pix), 3 * 97)}
print(len(colours), hashlib.sha256(pix).hexdigest()[:16], f"{w}x{h}")
PY
shot() {  # shot NAME -> prints "<colours> <sha> <WxH>"
    qmp "{\"execute\":\"screendump\",\"arguments\":{\"filename\":\"$C/$1.ppm\"}}"
    python3 "$C/shot.py" "$C/$1.ppm" "$VM_DIR/$1.png"
}

vmssh() {
    ssh -i "$C/key" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR \
        admin@127.0.0.1 "$@"
}

# ---- access -----------------------------------------------------------------
log "waiting for SSH with the consumer key..."
SECONDS=0
until vmssh true 2>/dev/null; do
    kill -0 "$qemu_pid" 2>/dev/null || { tail -60 "$VM_DIR/serial-consumer.log"; fail "QEMU exited"; }
    [ "$SECONDS" -lt 600 ] || { tail -60 "$VM_DIR/serial-consumer.log"; fail "no SSH after 600 s"; }
    sleep 5
done
log "SSH up after ${SECONDS}s"
if ssh -i "$VM_DIR/builder-key" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR \
        admin@127.0.0.1 true 2>/dev/null; then
    fail "the build key still logs in"
fi
[ "$(vmssh 'wc -l < ~/.ssh/authorized_keys')" = 1 ] || fail "authorized_keys is not just the consumer key"
# Capture first: grep -q exits early, and the SIGPIPE fails the pipe.
sshd_conf=$(vmssh 'sudo -n sshd -T' 2>&1 || true)
# OpenSSH 10 prints keywords in their documented case.
if ! grep -qix 'passwordauthentication no' <<< "$sshd_conf"; then
    grep -iE '^(passwordauthentication|kbdinteractiveauthentication) ' <<< "$sshd_conf" || head -5 <<< "$sshd_conf"
    fail "SSH password login is not disabled"
fi
vmssh 'sudo -n true' || fail "admin has no passwordless sudo"
for _ in $(seq 1 30); do
    python3 "$C/qmp.py" "$SOCK/qga" '{"execute":"guest-ping"}' 5 >/dev/null 2>&1 && break
    sleep 2
done
python3 "$C/qmp.py" "$SOCK/qga" '{"execute":"guest-ping"}' 5 >/dev/null || fail "QEMU guest agent does not answer"
# scripts/vm/vm-exec, qci's guest transport, runs commands with guest-exec.
python3 "$C/qmp.py" "$SOCK/qga" '{"execute":"guest-exec","arguments":{"path":"/usr/bin/true"}}' 5 \
    || fail "the guest agent refuses guest-exec"
pw_status=$(vmssh 'sudo -n passwd -S admin')
[ "$(echo "$pw_status" | cut -d' ' -f2)" = P ] || fail "admin password is not usable: $pw_status"
log "access: consumer key only, password SSH off, sudo, guest agent with guest-exec, admin password set"

# ---- identity ---------------------------------------------------------------
mid=$(vmssh 'cat /etc/machine-id')
[ -n "$mid" ] && [ "$mid" != "$(cat "$VM_DIR/build-machine-id")" ] || fail "machine-id was not regenerated ($mid)"
if [ "$(vmssh 'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub')" = "$(cat "$VM_DIR/build-hostkey")" ]; then
    log "WARN: SSH host key is the build's; copies of the image share it"
fi
log "identity: new machine-id $mid"

# ---- health -----------------------------------------------------------------
state=$(vmssh 'timeout 300 systemctl is-system-running --wait' || true)
if [ "$state" != running ]; then
    vmssh 'systemctl --no-pager --failed; sudo -n journalctl -b -p warning --no-pager | tail -80' || true
    fail "system state is '$state', not running"
fi
vmssh 'set -e
    for u in greetd qdistro-admin-broker qdistro-session-manager qdistro-pwd qdistro-root-exec.socket; do
        systemctl is-active --quiet "$u" || { echo "NOT ACTIVE: $u"; exit 1; }
    done
    names=$(busctl --system --acquired list --no-legend | cut -d" " -f1)
    for n in org.qdistro.AdminBroker1 org.qdistro.SessionManager1 org.qdistro.Pwd1; do
        printf "%s\n" "$names" | grep -qx "$n" || { echo "NOT ON BUS: $n"; exit 1; }
    done
    test "$(sudo -n cat /var/lib/qdistro/bootstrap/installer-chain.state | wc -l)" -eq 11' \
    || fail "qdistro services"
log "health: running, no failed units, services and bus names up, 11 chain steps"

# ---- packages ---------------------------------------------------------------
vmssh "set -e
    if ls /etc/zypp/services.d/*.service >/dev/null 2>&1; then exit 1; fi
    urls=\$(sed -n 's/^baseurl=//p' /etc/zypp/repos.d/*.repo)
    test -n \"\$urls\"
    printf '%s\n' \"\$urls\" | grep -v '/history/$SNAPSHOT/' && exit 1
    grep -qx 'SNAPSHOT=$SNAPSHOT' /etc/qdistro/test-substrate
    sudo -n zypper -n --quiet refresh
    sudo -n zypper -n install --dry-run --no-recommends bats jq >/dev/null" \
    || fail "package repositories are not usable or not pinned to $SNAPSHOT"
log "packages: only history/$SNAPSHOT repositories; test packages resolve"

# ---- desktop ----------------------------------------------------------------
for _ in $(seq 1 60); do
    vmssh 'pgrep -u _greeter -f qdgreeter >/dev/null' && break
    sleep 2
done
vmssh 'pgrep -u _greeter -f qdgreeter >/dev/null' || fail "qdgreeter is not running"
greeter=""
for _ in $(seq 1 30); do
    greeter=$(shot consumer-greeter)
    [ "${greeter%% *}" -gt 8 ] && break
    sleep 3
done
log "greeter screenshot: $greeter"
[ "${greeter%% *}" -gt 8 ] || fail "display stays blank under the greeter"
sleep 5   # the password field takes focus once the greeter has painted
for ch in $(printf '%s' "$PASSWORD" | fold -w1); do
    qmp "{\"execute\":\"send-key\",\"arguments\":{\"keys\":[{\"type\":\"qcode\",\"data\":\"$ch\"}]}}"
    sleep 0.15
done
qmp '{"execute":"send-key","arguments":{"keys":[{"type":"qcode","data":"ret"}]}}'
log "typed the password on the virtual keyboard"
# is-active with several units succeeds if ANY is active; check each one.
user_active() {
    local u
    for u in "$@"; do
        vmssh "sudo -n systemctl --user -M admin@ is-active --quiet $u" 2>/dev/null || return 1
    done
}
for _ in $(seq 1 60); do
    user_active qdwin-session.target && break
    sleep 2
done
if ! user_active qdwin-session.target qdwin-compositor.service qdshell.service qdlocker.service; then
    vmssh 'sudo -n systemctl --user -M admin@ --no-pager status qdwin-session.target qdwin-compositor.service qdshell.service qdlocker.service; sudo -n journalctl -b -u greetd --no-pager | tail -40' || true
    shot consumer-login-failed >/dev/null || true
    fail "greeter login did not start the qdwin session"
fi
log "session: qdwin-session.target, compositor, qdshell and qdlocker active"
# qdshell needs a moment to map and paint its layer surfaces; under TCG that
# can stretch well past a fixed sleep, so poll like the greeter check does.
desktop=""
for _ in $(seq 1 36); do
    sleep 5
    desktop=$(shot consumer-desktop)
    [ "${desktop%% *}" -gt 8 ] && break
done
log "desktop screenshot: $desktop"
if [ "${desktop%% *}" -le 8 ]; then
    # A uniform frame is ambiguous: a crashed or never-painted qdshell, a
    # rejected layer-shell bind, or plain TCG slowness all look identical in
    # the screendump. Dump the session journals so the run log can tell them
    # apart before failing.
    vmssh 'echo "--- qdshell journal"; sudo -n journalctl -b --no-pager _UID=1000 _SYSTEMD_USER_UNIT=qdshell.service -n 30 2>&1; \
           echo "--- compositor journal"; sudo -n journalctl -b --no-pager _COMM=weston 2>/dev/null | tail -30; \
           echo "--- qdlocker journal"; sudo -n journalctl -b --no-pager _UID=1000 _SYSTEMD_USER_UNIT=qdlocker.service -n 15 2>&1' || true
    fail "desktop screenshot is blank"
fi
[ "$(echo "$desktop" | cut -d' ' -f2)" != "$(echo "$greeter" | cut -d' ' -f2)" ] || fail "screen did not change after login"
user_active qdwin-compositor.service qdshell.service || fail "session did not stay up"

vmssh 'sudo -n systemctl poweroff' || true
for _ in $(seq 1 60); do kill -0 "$qemu_pid" 2>/dev/null || break; sleep 2; done
log "PASS"
