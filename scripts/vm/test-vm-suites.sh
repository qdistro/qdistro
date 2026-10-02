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
#           suites qci's host gate runs (ci/lib/gates/host.sh), including its
#           cross-batch mixed process, minus qdbrowser (QtWebEngine; no browser
#           in this image) and qdterm's printer test (excluded there too).
#   bats    every tests/integration/vm/*.bats from the runner against the
#           guest, through qci's own scripts/vm/vm-exec and the QEMU guest
#           agent (a virsh stand-in replaces libvirt). All files share one
#           guest, in order; qci gives each file its own disposable VM. After
#           each file the harness checks the core services and admin's session
#           and restarts what a file left stopped, noting it in that file's TAP.
#
# Before bats, the overlay gets what qci's test lane adds to the bootstrap
# chain (scripts/vm/fresh-vm-bootstrap.sh): the media, multimachine and
# template installers, /etc/qdistro/profile (dev), the qdistro-approvals CLI,
# the in-VM probes at /root/, the RDP certificate for the nested probes and
# the fixed test password Pa_ssw0rd45 for admin and root. None of that is in
# the shipped image. First admin logs in at the greeter, on QEMU's virtual
# keyboard with the image password, so the bats files find a live qdwin
# session on wayland-1.
#
# QDISTRO_SUITES (default "pytest bats") picks the phases;
# QDISTRO_SUITES_BATS_FILES (basenames, space-separated) narrows bats;
# QDISTRO_SUITES_BUDGET (seconds, default 12000) stops starting new suites
# once spent: the last one started (pytest 30 min, bats 20 min at most)
# still ends inside the workflow step's 240 minutes.
#
# Output: $VM_DIR/suites/ (junit XML, per-file bats TAP, summary.md).
# Exit status: 0 when every selected suite ran to completion, whatever its
# pass/fail counts (those are the result, in summary.md); 2 when a suite did
# not run or did not finish (crash, timeout, lost guest, budget); 1 when the
# harness itself failed.
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
BUDGET=${QDISTRO_SUITES_BUDGET:-12000}
PHASES=" ${QDISTRO_SUITES:-pytest bats} "
IMAGE_PASSWORD=${QDISTRO_TEST_VM_PASSWORD:-qdistro}
QCI_PASSWORD=Pa_ssw0rd45
# Test-only packages: the pytest suites' third-party imports and the tools the
# cloud test base installs for tests (install-deps.sh) that the image omits,
# including what the backup probes need for their own btrfs loopback
# filesystems and the RDP tools of the nested probes.
TEST_PKGS=(python313-pytest python313-pytest-qt python313-pytest-timeout
    python313-hypothesis python313-numpy python313-Pillow python313-textual
    python313-rich python313-jeepney python313-mistune python313-tomli-w
    python313-tomli python313-pyte python313-pyenchant python313-matplotlib
    python313-networkx python313-qrcode python313-setproctitle
    python313-Pygments python313-mcp python313-dbus_next
    python313-pytest-asyncio bzip2 myspell-en_US
    bats jq tesseract-ocr ydotool Mesa-demo-egl rsync git-core
    rage-encryption btrfsprogs freerdp freerdp-server)

log() { echo "[suites] $*"; }
START=$(date +%s)
budget_left() { echo $((BUDGET - ($(date +%s) - START))); }

for tool in bats jq qemu-img qemu-system-x86_64 cloud-localds ssh python3; do
    command -v "$tool" >/dev/null || { log "FATAL: $tool is not installed on the runner"; exit 1; }
done

rm -rf "$OUT"
mkdir -p "$OUT/junit" "$OUT/bats"
W=$OUT/vm
mkdir -p "$W"

# ---- what will run: written first, so a run that stops early is visible -----
# name|dir|args. "unit" runs tests/unit 30 files per process, as the host gate
# does (one native crash costs one batch); "unit-mixed" is the host gate's
# deliberate mixed process across those batch boundaries.
SUITES=(
    "unit|.|@batch30"
    "unit-mixed|.|tests/unit/test_admin_widgets_logic.py tests/unit/test_broker_subscriber_restart.py tests/unit/test_broker_upload_lineage.py"
    "presentation|sdk/presentation|"
    "qdgreeter|qdgreeter|tests"
    "qdlocker|qdlocker|tests/unit"
    "qdfileman|qdfileman|"
    "qnotebook|qnotebook|"
    "qdterm|qdterm|--ignore=tests/test_print_terminal.py"
)
[[ $PHASES == *" pytest "* ]] || SUITES=()
BATS_FILES=()
if [[ $PHASES == *" bats "* ]]; then
    if [ -n "${QDISTRO_SUITES_BATS_FILES:-}" ]; then
        for b in $QDISTRO_SUITES_BATS_FILES; do BATS_FILES+=("$REPO/tests/integration/vm/$b.bats"); done
    else
        BATS_FILES=("$REPO"/tests/integration/vm/*.bats)
    fi
fi
{
    echo "commit $(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
    for s in "${SUITES[@]}"; do echo "pytest ${s%%|*}"; done
    for f in "${BATS_FILES[@]}"; do echo "bats $(basename "$f" .bats)"; done
} > "$OUT/expected"

summarize() {
    python3 "$REPO/scripts/vm/test-vm-suites-summary.py" "$OUT" > "$OUT/summary.md"
}
SOCK=$(mktemp -d /tmp/qdsuites.XXXXXX)
qemu_pid=""
finish() {
    local rc=$?
    [ -z "$qemu_pid" ] || kill "$qemu_pid" 2>/dev/null || true
    rm -rf "$W/disk.qcow2" "$SOCK"
    if [ ! -s "$OUT/summary.md" ]; then
        summarize || true
        [ "$rc" -ne 0 ] || rc=1
    fi
    exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM INT

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
# send_text TEXT: type on QEMU's virtual keyboard (letters, digits, '_').
send_text() {
    local ch keys
    for ch in $(printf '%s' "$1" | fold -w1); do
        case "$ch" in
            [a-z0-9]) keys="{\"type\":\"qcode\",\"data\":\"$ch\"}" ;;
            [A-Z]) keys="{\"type\":\"qcode\",\"data\":\"shift\"},{\"type\":\"qcode\",\"data\":\"${ch,}\"}" ;;
            _) keys='{"type":"qcode","data":"shift"},{"type":"qcode","data":"minus"}' ;;
            *) log "send_text: cannot type '$ch'"; return 1 ;;
        esac
        qmp "{\"execute\":\"send-key\",\"arguments\":{\"keys\":[$keys]}}"
        sleep 0.15
    done
    qmp '{"execute":"send-key","arguments":{"keys":[{"type":"qcode","data":"ret"}]}}'
}

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
# The guest lists a suite's process tags in <suite>.tags up front and records
# <tag>.rc beside <tag>.xml: the summary counts a suite complete only when
# every listed process finished (pytest exit 0 or 1) and wrote its junit.
cat > "$W/run-pytest.sh" <<'GUEST'
#!/bin/bash
# In the guest, as admin: run one suite; results land in /home/admin/junit.
set -u
name=$1 dir=$2 args=$3
J=/home/admin/junit
mkdir -p "$J"
cd "/home/admin/qdistro-test/$dir" || exit 2
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1
run() {  # run TAG ARGS...
    local rc=0
    timeout -k 30 1800 dbus-run-session -- python3 -m pytest -p no:cacheprovider -q \
        --junitxml="$J/$1.xml" "${@:2}" || rc=$?
    echo "$rc" > "$J/$1.rc"
    [ "$rc" -le 1 ]
}
ok=0
if [ "$args" = @batch30 ]; then
    mapfile -t files < <(find tests/unit -name 'test_*.py' | sort)
    for ((i = 0; i < ${#files[@]}; i += 30)); do echo "$name-$((i / 30 + 1))"; done > "$J/$name.tags"
    for ((i = 0; i < ${#files[@]}; i += 30)); do
        run "$name-$((i / 30 + 1))" "${files[@]:i:30}" || ok=1
    done
else
    echo "$name" > "$J/$name.tags"
    # shellcheck disable=SC2086 # args is a word list
    run "$name" $args || ok=1
fi
exit "$ok"
GUEST
vmssh 'cat > run-pytest.sh' < "$W/run-pytest.sh"
for s in "${SUITES[@]}"; do
    IFS='|' read -r name dir args <<< "$s"
    if [ "$(budget_left)" -le 0 ]; then log "budget spent; pytest $name not started"; break; fi
    log "pytest $name..."
    SECONDS=0
    if vmssh "bash run-pytest.sh '$name' '$dir' '$args'" > "$OUT/pytest-$name.log" 2>&1; then rc=0; else rc=$?; fi
    log "pytest $name: $([ "$rc" = 0 ] && echo completed || echo "rc=$rc") (${SECONDS}s)"
done
if [ "${#SUITES[@]}" -gt 0 ]; then
    vmssh 'tar -C /home/admin/junit -cf - .' | tar -C "$OUT/junit" -xf - \
        || log "could not fetch the junit results"
fi

# ---- bats, from the runner ---------------------------------------------------
if [ "${#BATS_FILES[@]}" -gt 0 ]; then
    # What a qci test lane gives its VM on top of the bootstrap chain
    # (fresh-vm-bootstrap.sh): the dev profile file (tier-2 launchers default
    # to the hardened profile without it), the media, multimachine and
    # template installers, the approvals CLI, the probes in a 0755 /root
    # (section 4b) and the RDP certificate of the nested probes (4c).
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
        done
        g=$(id -gn admin); d=/home/admin/qdwin-rdp
        install -d -o admin -g "$g" -m 0700 "$d"
        runuser -u admin -- winpr-makecert -rdp -path "$d" >/dev/null
        for f in "$d"/*.crt; do [ "$f" = "$d/rdp.crt" ] || mv "$f" "$d/rdp.crt"; done
        for f in "$d"/*.key; do [ "$f" = "$d/rdp.key" ] || mv "$f" "$d/rdp.key"; done
        chown admin:"$g" "$d"/rdp.crt "$d"/rdp.key
        chmod 0600 "$d/rdp.key"; chmod 0644 "$d/rdp.crt"' > "$OUT/qci-lane-setup.log" 2>&1 \
        || { tail -30 "$OUT/qci-lane-setup.log"; exit 1; }

    # admin logs in at the greeter as a tester would, with the image password.
    session_up() {
        rootssh 'systemctl --user -M admin@ is-active --quiet qdwin-session.target &&
            test -S /run/user/1000/wayland-1' 2>/dev/null
    }
    SECONDS=0
    until vmssh 'pgrep -u _greeter -f qdgreeter >/dev/null'; do
        [ "$SECONDS" -lt 120 ] || { log "qdgreeter is not running"; exit 1; }
        sleep 2
    done
    sleep 10   # the password field takes focus once the greeter has painted
    send_text "$IMAGE_PASSWORD"
    SECONDS=0
    until session_up; do
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
"""virsh stand-in: qemu-agent-command, domuuid and screenshot over QEMU sockets.

Like libvirt, one guest-agent transaction at a time (a lock shared by every
caller), and each starts by resynchronising the stream: a 0xFF byte resets the
agent's parser, and guest-sync-delimited makes the agent mark its reply with
0xFF, so a partial or stale reply left by an interrupted caller is skipped.
An agent error is printed as virsh prints it (stderr, exit 1); vm-exec then
behaves exactly as it does under qci.
"""
import fcntl, json, os, random, socket, sys, time

DEADLINE = time.monotonic() + 25      # inside vm-exec's 30 s cap per RPC
args = sys.argv[1:]
if args[:1] == ["-c"]:
    args = args[2:]
sock = os.environ["QDSUITES_SOCK"]

def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)

def left():
    t = DEADLINE - time.monotonic()
    if t <= 0:
        raise TimeoutError("deadline")
    return t

class Stream:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(left())
        self.s.connect(path)
        self.buf = b""

    def send(self, data):
        self.s.settimeout(left())
        self.s.sendall(data)

    def _fill(self):
        self.s.settimeout(left())
        chunk = self.s.recv(65536)
        if not chunk:
            raise ConnectionError("closed")
        self.buf += chunk

    def skip_to_delimiter(self):
        while b"\xff" not in self.buf:
            self.buf = b""
            self._fill()
        self.buf = self.buf.split(b"\xff", 1)[1]

    def message(self):
        """Next complete JSON reply; unparsable lines are skipped."""
        while True:
            while b"\n" not in self.buf:
                self._fill()
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.replace(b"\xff", b"").strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and ("return" in r or "error" in r):
                return r

def call(st, cmd):
    st.send(json.dumps(cmd).encode() + b"\n")
    return st.message()

def agent(cmd):
    lock = open(f"{sock}/qga.lock", "w")
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            left()
            time.sleep(0.05)
    st = Stream(f"{sock}/qga")
    sync = random.randrange(1, 2**31)
    st.send(b"\xff" + json.dumps({"execute": "guest-sync-delimited",
                                  "arguments": {"id": sync}}).encode() + b"\n")
    while True:
        st.skip_to_delimiter()
        r = st.message()
        if r.get("return") == sync:
            break
    return call(st, cmd)

if not args:
    die("no command")
if args[0] == "qemu-agent-command":
    if len(args) < 3:
        die("usage: qemu-agent-command DOMAIN JSON")
    cmd = json.loads(args[2])
    try:
        r = agent(cmd)
    except (OSError, TimeoutError, ConnectionError) as e:
        die(f"Guest agent is not responding: {e}")
    if "error" in r:
        die(f"guest agent command failed: unable to execute QEMU agent command "
            f"'{cmd.get('execute')}': {r['error'].get('desc', '')}")
    print(json.dumps(r, separators=(",", ":")))   # compact, as virsh prints it
elif args[0] == "domuuid":
    print("00000000-0000-4000-8000-00000000d15c")
elif args[0] == "screenshot" and len(args) >= 3:
    try:
        st = Stream(f"{sock}/qmp")
        call(st, {"execute": "qmp_capabilities"})     # message() skips the greeting
        r = call(st, {"execute": "screendump",
                      "arguments": {"filename": os.path.abspath(args[2])}})
    except (OSError, TimeoutError, ConnectionError) as e:
        die(f"QMP: {e}")
    if "error" in r:
        die(r["error"].get("desc", "screendump failed"))
    print(f"Screenshot saved to {args[2]}, with type of image/x-portable-pixmap")
else:
    die(f"virsh stand-in: '{args[0]}' is not supported (no libvirt in this runner)")
PY
chmod +x "$W/bin/virsh"

# After each file: the core system services and admin's session, as the
# consumer check requires them. What a file left stopped is restarted and
# noted in its TAP, so a later file is not judged on an earlier one's state.
baseline() {
    rootssh 'for u in greetd qdistro-admin-broker qdistro-session-manager qdistro-pwd qdistro-root-exec.socket; do
            systemctl is-active --quiet "$u" && continue
            echo "harness: $u was not active after this file; restarting it"
            systemctl reset-failed "$u" 2>/dev/null; systemctl start "$u" || echo "harness: $u did not start"
        done
        for u in qdwin-session.target qdwin-compositor.service qdshell.service; do
            systemctl --user -M admin@ is-active --quiet "$u" && continue
            echo "harness: admin $u was not active after this file; starting qdwin-session.target"
            systemctl --user -M admin@ start qdwin-session.target || echo "harness: qdwin-session.target did not start"
            break
        done' 2>&1
}

for f in "${BATS_FILES[@]}"; do
    base=$(basename "$f" .bats)
    if [ "$(budget_left)" -le 0 ]; then log "budget spent; bats $base and later files not started"; break; fi
    log "bats $base..."
    SECONDS=0
    if (cd "$REPO" && PATH="$W/bin:$PATH" QDSUITES_SOCK="$SOCK" VM_NAME=qdistro-suites \
            env -u QDISTRO_PROFILE -u VM_SSH_PORT -u VM_EXEC \
            timeout -k 30 "$BATS_TIMEOUT" bats --tap "$f") \
            > "$OUT/bats/$base.tap" 2>&1; then rc=0; else rc=$?; fi
    echo "# rc=$rc seconds=$SECONDS" >> "$OUT/bats/$base.tap"
    log "bats $base rc=$rc (${SECONDS}s)"
    # A file that wedged or rebooted the guest must not poison the rest; the
    # summary lists every file after it as not run.
    if ! rootssh true 2>/dev/null; then
        log "guest unreachable after $base; stopping bats"
        break
    fi
    baseline | sed 's/^/# /' | tee -a "$OUT/bats/$base.tap"
done

rootssh 'systemctl poweroff' 2>/dev/null || true
for _ in $(seq 1 60); do kill -0 "$qemu_pid" 2>/dev/null || break; sleep 2; done

if summarize; then rc=0; else rc=$?; fi
cat "$OUT/summary.md"
exit "$rc"
