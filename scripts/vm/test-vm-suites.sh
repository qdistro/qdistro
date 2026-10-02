#!/bin/bash
# test-vm-suites.sh — run qdistro's pytest and VM bats suites against the
# shipped test VM. Run on the CI runner by .github/workflows/qdistro-test-vm.yml
# after the image is final.
#
#   test-vm-suites.sh VM_DIR VM_IMAGE QEMU_ACCEL
#
# The guest boots a throwaway overlay (the image is never written), as a
# consumer would, and the test-only tools go into that overlay: the shipped
# image stays runtime-only.
#
#   pytest  inside the guest, as admin, on a copy of /root/qdistro-src: the
#           suites qci's host gate runs (ci/lib/gates/host.sh), minus
#           qdbrowser (QtWebEngine; no browser in this image).
#   bats    tests/integration/vm/*.bats from the runner against the guest,
#           through qci's own scripts/vm/vm-exec and the QEMU guest agent
#           (a virsh stand-in replaces libvirt). All files share one guest,
#           in order; qci gives each file its own disposable VM.
#
# Before bats, the overlay gets what qci's test lane adds to the bootstrap
# chain (scripts/vm/fresh-vm-bootstrap.sh): the media, multimachine and
# template installers, /etc/qdistro/profile (dev), the qdistro-approvals CLI,
# the in-VM probes at /root/ and the fixed test password Pa_ssw0rd45 for admin and root. None of that is
# in the shipped image. First admin logs in at the greeter, on QEMU's virtual
# keyboard with the image password, so the bats files find a live qdwin
# session on wayland-1.
#
# QDISTRO_SUITES (default "pytest bats") picks the phases;
# QDISTRO_SUITES_BATS_FILES (basenames, space-separated) narrows bats.
# Output: $VM_DIR/suites/ (junit XML, per-file bats TAP logs, summary.md).
# Exit status: 0 when every suite ran; the pass/fail counts are the result,
# reported in summary.md (and the job summary on GitHub).
set -euo pipefail

VM_DIR=${1:?VM_DIR}
VM_IMAGE=${2:?VM_IMAGE}
ACCEL=${3:?QEMU_ACCEL}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
OUT=$VM_DIR/suites
PORT=${QDISTRO_SUITES_SSH_PORT:-2224}
OVMF_CODE=${OVMF_CODE:-/usr/share/OVMF/OVMF_CODE_4M.fd}
OVMF_VARS=${OVMF_VARS:-/usr/share/OVMF/OVMF_VARS_4M.fd}
BATS_TIMEOUT=${QDISTRO_SUITES_BATS_TIMEOUT:-1200}
PHASES=" ${QDISTRO_SUITES:-pytest bats} "
IMAGE_PASSWORD=${QDISTRO_TEST_VM_PASSWORD:-qdistro}
QCI_PASSWORD=Pa_ssw0rd45
# Test-only packages: the pytest suites' third-party imports and the tools the
# cloud test base installs for tests (install-deps.sh) that the image omits.
TEST_PKGS=(python313-pytest python313-pytest-qt python313-pytest-timeout
    python313-hypothesis python313-numpy python313-Pillow python313-textual
    python313-rich python313-jeepney python313-mistune python313-tomli-w
    python313-tomli python313-pyte python313-pyenchant python313-matplotlib
    python313-networkx python313-qrcode python313-setproctitle
    python313-Pygments python313-mcp python313-dbus_next
    python313-pytest-asyncio bzip2 myspell-en_US
    bats jq tesseract-ocr ydotool Mesa-demo-egl rsync git-core)

log() { echo "[suites] $*"; }

rm -rf "$OUT"
mkdir -p "$OUT/junit" "$OUT/bats"
W=$OUT/vm
mkdir -p "$W"
SOCK=$(mktemp -d /tmp/qdsuites.XXXXXX)
qemu-img create -q -f qcow2 -b "$VM_IMAGE" -F qcow2 "$W/disk.qcow2"
ssh-keygen -q -t ed25519 -N '' -C suites -f "$W/key"
printf '#cloud-config\nssh_authorized_keys:\n  - %s\n' "$(cat "$W/key.pub")" > "$W/user-data"
printf 'instance-id: qdistro-suites-1\nlocal-hostname: qdistro-suites\n' > "$W/meta-data"
cloud-localds "$W/seed.img" "$W/user-data" "$W/meta-data"
cp "$OVMF_VARS" "$W/OVMF_VARS.fd"
qemu-system-x86_64 -machine "q35,accel=$ACCEL" -cpu max -m 8192 -smp "$(nproc)" \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$W/OVMF_VARS.fd" \
    -drive if=virtio,format=qcow2,file="$W/disk.qcow2" \
    -drive if=virtio,format=raw,readonly=on,file="$W/seed.img" \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:"$PORT"-:22 \
    -device virtio-net-pci,netdev=net0 \
    -device virtio-vga \
    -device virtio-serial-pci \
    -chardev socket,id=qga0,path="$SOCK/qga",server=on,wait=off \
    -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0 \
    -qmp unix:"$SOCK/qmp",server=on,wait=off \
    -display none -serial file:"$VM_DIR/serial-suites.log" -monitor none \
    > "$VM_DIR/qemu-suites.log" 2>&1 &
qemu_pid=$!
trap 'kill "$qemu_pid" 2>/dev/null || true; rm -rf "$W/disk.qcow2" "$SOCK"' EXIT

# One QMP command per connection.
cat > "$W/qmp.py" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.settimeout(30)
s.connect(sys.argv[1])
f = s.makefile("rw")
def call(c):
    f.write(json.dumps(c) + "\n"); f.flush()
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r:
            return r
json.loads(f.readline())          # greeting
call({"execute": "qmp_capabilities"})
sys.exit(1 if "error" in call(json.loads(sys.argv[2])) else 0)
PY
qmp() { python3 "$W/qmp.py" "$SOCK/qmp" "$1"; }

ssh_opts=(-i "$W/key" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o ServerAliveInterval=30
    -o LogLevel=ERROR)
vmssh() { ssh "${ssh_opts[@]}" admin@127.0.0.1 "$@"; }
rootssh() { ssh "${ssh_opts[@]}" root@127.0.0.1 "$@"; }

SECONDS=0
until vmssh true 2>/dev/null; do
    kill -0 "$qemu_pid" 2>/dev/null || { tail -40 "$VM_DIR/serial-suites.log"; exit 1; }
    [ "$SECONDS" -lt 600 ] || { tail -40 "$VM_DIR/serial-suites.log"; exit 1; }
    sleep 5
done
log "guest up after ${SECONDS}s"

# Test harness access and tools, in the overlay only.
vmssh 'sudo -n sh -c "set -e; install -d -m 0700 /root/.ssh; \
    install -m 0600 /home/admin/.ssh/authorized_keys /root/.ssh/authorized_keys"'
rootssh true
log "installing ${#TEST_PKGS[@]} test packages..."
rootssh "zypper -n --quiet install --no-recommends ${TEST_PKGS[*]}" > "$OUT/test-packages.log" 2>&1 \
    || { tail -30 "$OUT/test-packages.log"; exit 1; }
rootssh 'set -e; rm -rf /home/admin/qdistro-test; cp -a /root/qdistro-src /home/admin/qdistro-test;
    chown -R admin: /home/admin/qdistro-test'

# ---- pytest, inside the guest ------------------------------------------------
# name|dir|mode|args. mode "batch30" mirrors the host gate's tests/unit run
# (30 files per process, so one native crash costs one batch).
SUITES=(
    "unit|.|batch30|"
    "presentation|sdk/presentation|all|"
    "qdgreeter|qdgreeter|all|tests"
    "qdlocker|qdlocker|all|tests/unit"
    "qdfileman|qdfileman|all|"
    "qnotebook|qnotebook|all|"
    "qdterm|qdterm|all|--ignore=tests/test_print_terminal.py"
)
cat > "$W/run-pytest.sh" <<'GUEST'
#!/bin/bash
# In the guest, as admin: run one suite; junit lands in /home/admin/junit.
set -u
name=$1 dir=$2 mode=$3 args=$4
cd "/home/admin/qdistro-test/$dir" || exit 2
mkdir -p /home/admin/junit
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1
run() {  # run TAG ARGS...
    timeout 1800 dbus-run-session -- python3 -m pytest -p no:cacheprovider -q \
        --junitxml="/home/admin/junit/$1.xml" "${@:2}"
}
rc=0
if [ "$mode" = batch30 ]; then
    mapfile -t files < <(find tests/unit -name 'test_*.py' | sort)
    for ((i = 0; i < ${#files[@]}; i += 30)); do
        run "$name-$((i / 30 + 1))" "${files[@]:i:30}" || rc=1
    done
else
    # shellcheck disable=SC2086 # args is a word list
    run "$name" $args || rc=1
fi
exit "$rc"
GUEST
vmssh 'cat > run-pytest.sh' < "$W/run-pytest.sh"
[[ $PHASES == *" pytest "* ]] || SUITES=()
for s in "${SUITES[@]}"; do
    IFS='|' read -r name dir mode args <<< "$s"
    log "pytest $name..."
    SECONDS=0
    if vmssh "bash run-pytest.sh '$name' '$dir' '$mode' '$args'" > "$OUT/pytest-$name.log" 2>&1; then rc=0; else rc=$?; fi
    log "pytest $name rc=$rc (${SECONDS}s)"
done
if [ "${#SUITES[@]}" -gt 0 ]; then
    vmssh 'tar -C /home/admin/junit -cf - .' | tar -C "$OUT/junit" -xf -
fi

# ---- bats, from the runner ---------------------------------------------------
# file|reason: files that need what this image leaves out on purpose. They are
# listed as not run in summary.md, never silently dropped.
NOT_RUN=(
    "browser-9e-daemons|needs a browser (none in the test VM)"
    "backup-btrfs-e2e|btrfs backups: the snapshots step is left out (XFS root)"
    "backup-driver-e2e|btrfs backups: the snapshots step is left out (XFS root)"
    "backup-rehearse-e2e|btrfs backups: the snapshots step is left out (XFS root)"
    "backup-rehearse-ssh-e2e|btrfs backups: the snapshots step is left out (XFS root)"
    "backup-ssh-e2e|btrfs backups: the snapshots step is left out (XFS root)"
)
BATS_FILES=()
if [[ $PHASES == *" bats "* ]]; then
    if [ -n "${QDISTRO_SUITES_BATS_FILES:-}" ]; then
        for b in $QDISTRO_SUITES_BATS_FILES; do BATS_FILES+=("$REPO/tests/integration/vm/$b.bats"); done
    else
        BATS_FILES=("$REPO"/tests/integration/vm/*.bats)
    fi
fi

if [ "${#BATS_FILES[@]}" -gt 0 ]; then
    # What a qci test lane gives its VM on top of the bootstrap chain
    # (fresh-vm-bootstrap.sh): the dev profile file (tier-2 launchers default
    # to the hardened profile without it), the media, multimachine and
    # template installers, the approvals CLI and the probes in a 0755 /root.
    rootssh 'set -e; Q=/root/qdistro-src; cd "$Q"
        printf "QDISTRO_PROFILE=dev\n" > /etc/qdistro/profile; chmod 0644 /etc/qdistro/profile
        for i in "install-media-for-vm.sh $Q/media" \
                 "install-multimachine-for-vm.sh $Q/multimachine" \
                 "install-templates-for-vm.sh $Q"; do
            set -- $i; bash "scripts/install/$1" "$2"
        done
        install -m 0755 cli/qdistro_approvals.py /usr/local/sbin/qdistro-approvals
        install -d -m 0755 /root
        for f in tests/integration/vm/probes/*.sh; do install -m 0755 "$f" "/root/${f##*/}"; done
        for f in tests/integration/vm/probes/*.py tests/integration/vm/probes/*.c; do
            install -m 0644 "$f" "/root/${f##*/}"
        done' > "$OUT/qci-lane-setup.log" 2>&1 \
        || { tail -30 "$OUT/qci-lane-setup.log"; exit 1; }

    # admin logs in at the greeter as a tester would, with the image password.
    SECONDS=0
    until vmssh 'pgrep -u _greeter -f qdgreeter >/dev/null'; do
        [ "$SECONDS" -lt 120 ] || { log "qdgreeter is not running"; exit 1; }
        sleep 2
    done
    sleep 10   # the password field takes focus once the greeter has painted
    for ch in $(printf '%s' "$IMAGE_PASSWORD" | fold -w1); do
        qmp "{\"execute\":\"send-key\",\"arguments\":{\"keys\":[{\"type\":\"qcode\",\"data\":\"$ch\"}]}}"
        sleep 0.15
    done
    qmp '{"execute":"send-key","arguments":{"keys":[{"type":"qcode","data":"ret"}]}}'
    SECONDS=0
    until rootssh 'systemctl --user -M admin@ is-active --quiet qdwin-session.target &&
            test -S /run/user/1000/wayland-1' 2>/dev/null; do
        [ "$SECONDS" -lt 120 ] || { log "greeter login did not start the qdwin session"; exit 1; }
        sleep 2
    done
    log "admin session up on wayland-1 after ${SECONDS}s"
    # The bats drivers authenticate with qci's fixed test password.
    rootssh "printf '%s\n' 'admin:$QCI_PASSWORD' 'root:$QCI_PASSWORD' | chpasswd"
fi

# The bats files reach the guest as in qci: helpers.bash's vm_run calls
# scripts/vm/vm-exec, which drives the QEMU guest agent through `virsh
# qemu-agent-command`. There is no libvirt here, so this stand-in for virsh
# answers the calls vm-exec and the files make on QEMU's own sockets. (The SSH
# transport is not equivalent: a root SSH login already has a logind session,
# so `runuser -l admin` gets no user bus and rootless podman cannot start.)
mkdir -p "$W/bin"
cat > "$W/bin/virsh" <<'PY'
#!/usr/bin/env python3
"""virsh stand-in: qemu-agent-command, domuuid and screenshot over QEMU sockets."""
import json, os, random, socket, sys

args = sys.argv[1:]
if args[:1] == ["-c"]:
    args = args[2:]
sock = os.environ["QDSUITES_SOCK"]

def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)

def session(path, timeout):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    s.connect(path)
    return s.makefile("rw")

def call(f, cmd, want=None):
    f.write(json.dumps(cmd) + "\n"); f.flush()
    while True:
        line = f.readline()
        if not line:
            die("guest agent closed the connection")
        line = line.lstrip("\xff").strip()
        if not line:
            continue
        r = json.loads(line)
        if "return" not in r and "error" not in r:
            continue                 # QMP event
        if want is not None and r.get("return") != want:
            continue                 # a stale reply from an abandoned call
        return r

if not args:
    die("no command")
if args[0] == "qemu-agent-command":
    if len(args) < 3:
        die("usage: qemu-agent-command DOMAIN JSON")
    cmd = json.loads(args[2])
    try:
        f = session(f"{sock}/qga", 60)
        sync = random.randrange(1, 2**31)
        call(f, {"execute": "guest-sync", "arguments": {"id": sync}}, want=sync)
        r = call(f, cmd)
    except (OSError, ValueError) as e:
        die(f"Guest agent is not responding: {e}")
    if "error" in r:
        die(f"internal error: unable to execute QEMU agent command "
            f"'{cmd.get('execute')}': {r['error'].get('desc', '')}")
    print(json.dumps(r, separators=(",", ":")))   # compact, as virsh prints it
elif args[0] == "domuuid":
    print("00000000-0000-4000-8000-00000000d15c")
elif args[0] == "screenshot" and len(args) >= 3:
    f = session(f"{sock}/qmp", 30)
    f.readline()
    call(f, {"execute": "qmp_capabilities"})
    r = call(f, {"execute": "screendump",
                 "arguments": {"filename": os.path.abspath(args[2])}})
    if "error" in r:
        die(r["error"].get("desc", "screendump failed"))
    print(f"Screenshot saved to {args[2]}, with type of image/x-portable-pixmap")
else:
    die(f"virsh stand-in: '{args[0]}' is not supported (no libvirt in this runner)")
PY
chmod +x "$W/bin/virsh"

for f in "${BATS_FILES[@]}"; do
    base=$(basename "$f" .bats)
    for entry in "${NOT_RUN[@]}"; do
        if [ "${entry%%|*}" = "$base" ]; then
            echo "${entry#*|}" > "$OUT/bats/$base.not-run"
            log "bats $base not run: ${entry#*|}"
            continue 2
        fi
    done
    log "bats $base..."
    SECONDS=0
    if (cd "$REPO" && PATH="$W/bin:$PATH" QDSUITES_SOCK="$SOCK" VM_NAME=qdistro-suites \
            env -u QDISTRO_PROFILE -u VM_SSH_PORT -u VM_EXEC \
            timeout "$BATS_TIMEOUT" bats --tap "$f") \
            > "$OUT/bats/$base.tap" 2>&1; then rc=0; else rc=$?; fi
    echo "# rc=$rc seconds=$SECONDS" >> "$OUT/bats/$base.tap"
    log "bats $base rc=$rc (${SECONDS}s)"
    # A file that wedged or rebooted the guest must not poison the rest.
    if ! rootssh true 2>/dev/null; then
        log "guest unreachable after $base; stopping bats"
        break
    fi
done

rootssh 'systemctl poweroff' 2>/dev/null || true
for _ in $(seq 1 60); do kill -0 "$qemu_pid" 2>/dev/null || break; sleep 2; done

python3 "$REPO/scripts/vm/test-vm-suites-summary.py" "$OUT" | tee "$OUT/summary.md"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then cat "$OUT/summary.md" >> "$GITHUB_STEP_SUMMARY"; fi
