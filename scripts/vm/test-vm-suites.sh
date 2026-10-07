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
# QDISTRO_SUITES_BUDGET (seconds, default 12000) caps the suites: none starts
# after it is spent, and each one is cut to what is left of it. The
# workflow sizes it from the time its job has left.
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
TEST_PKGS=(python314-pytest python314-pytest-qt python314-pytest-timeout
    python314-hypothesis python314-numpy python314-Pillow python314-textual
    python314-rich python314-jeepney python314-mistune python314-tomli-w
    python314-tomli python314-pyte python314-pyenchant python314-matplotlib
    python314-networkx python314-qrcode python314-setproctitle
    python314-Pygments python314-mcp python314-dbus_next
    python314-pytest-asyncio bzip2 myspell-en_US
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
ssh_opts=(-i "$W/key" -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o ServerAliveInterval=30
    -o LogLevel=ERROR)
# Every guest command is bounded: SSH_CAP seconds (default 120), set higher
# for the few long setup steps.
vmssh() { timeout -k 10 "${SSH_CAP:-120}" ssh "${ssh_opts[@]}" admin@127.0.0.1 "$@"; }
rootssh() { timeout -k 10 "${SSH_CAP:-120}" ssh "${ssh_opts[@]}" root@127.0.0.1 "$@"; }

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
        # shell-modules wrecks the shared guest by design: its per-test setup
        # stops the broker and its failing probes leave their own outer
        # compositor. qci never notices (a disposable VM per file); here it
        # runs last so its debris cannot cut the files after it.
        for i in "${!BATS_FILES[@]}"; do
            [ "$(basename "${BATS_FILES[i]}")" = shell-modules.bats ] || continue
            BATS_FILES+=("${BATS_FILES[i]}")
            unset 'BATS_FILES[i]'
        done
        BATS_FILES=("${BATS_FILES[@]}")
    fi
fi
{
    echo "commit $(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "phases${PHASES% }"
    [ -z "${QDISTRO_SUITES_BATS_FILES:-}" ] || echo "bats-filter $QDISTRO_SUITES_BATS_FILES"
    for s in "${SUITES[@]}"; do echo "pytest ${s%%|*}"; done
    for f in "${BATS_FILES[@]}"; do echo "bats $(basename "$f" .bats)"; done
} > "$OUT/expected"

summarize() {
    python3 "$REPO/scripts/vm/test-vm-suites-summary.py" "$OUT" > "$OUT/summary.md"
}
SOCK=$(mktemp -d /tmp/qdsuites.XXXXXX)
qemu_pid=""
ended=""        # "complete" or "incomplete" once the summary decided
interrupted=""
fetch_junit() {  # pytest results so far, from the guest (bounded)
    timeout -k 5 60 ssh "${ssh_opts[@]}" admin@127.0.0.1 \
        'tar -C /home/admin/junit -cf - . 2>/dev/null' | tar -C "$OUT/junit" -xf - 2>/dev/null
}
# Exit status: 0 complete, 2 incomplete (including an interrupted run), 1 any
# other harness failure.
finish() {
    local rc=$?
    trap - TERM INT
    if [ -z "$ended" ] && [ -n "$qemu_pid" ] && kill -0 "$qemu_pid" 2>/dev/null \
            && [ "${#SUITES[@]}" -gt 0 ]; then
        fetch_junit || true
    fi
    [ -z "$qemu_pid" ] || kill "$qemu_pid" 2>/dev/null || true
    rm -rf "$W/disk.qcow2" "$SOCK"
    case "$ended" in
        complete) exit 0 ;;
        incomplete) exit 2 ;;
    esac
    summarize || true
    [ -n "$interrupted" ] && exit 2
    echo "[suites] harness failed (status $rc)" >&2
    exit 1
}
trap finish EXIT
trap 'interrupted=1; exit 2' TERM INT
# cap N: N seconds, or less when the budget has less left.
cap() { local l; l=$(budget_left); [ "$l" -gt 1 ] || l=1; [ "$l" -lt "$1" ] && echo "$l" || echo "$1"; }
# A setup step failed: incomplete (2) when the budget cut it, harness (1) otherwise.
setup_failed() {
    if [ "$(budget_left)" -le 0 ]; then log "setup cut by the time budget"; interrupted=1; exit 2; fi
    exit 1
}
if [ "$(budget_left)" -le 0 ]; then
    log "no time budget left; nothing started"
    interrupted=1
    exit 2
fi

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

SECONDS=0
until SSH_CAP=$(cap 120) vmssh true 2>/dev/null; do
    kill -0 "$qemu_pid" 2>/dev/null || { tail -40 "$VM_DIR/serial-suites.log"; exit 1; }
    # Budget before the boot ceiling: a spent budget at 600 s is an
    # incomplete run, not a harness failure.
    [ "$(budget_left)" -gt 0 ] || setup_failed
    [ "$SECONDS" -lt 600 ] || { tail -40 "$VM_DIR/serial-suites.log"; exit 1; }
    sleep 5
done
log "guest up after ${SECONDS}s"

# Test harness access and tools, in the overlay only.
SSH_CAP=$(cap 120) vmssh 'sudo -n sh -c "set -e; install -d -m 0700 /root/.ssh; \
    install -m 0600 /home/admin/.ssh/authorized_keys /root/.ssh/authorized_keys"' || setup_failed
SSH_CAP=$(cap 60) rootssh true || setup_failed
log "installing ${#TEST_PKGS[@]} test packages..."
SSH_CAP=$(cap 1800) rootssh "zypper -n --quiet install --no-recommends ${TEST_PKGS[*]}" > "$OUT/test-packages.log" 2>&1 \
    || { tail -30 "$OUT/test-packages.log"; setup_failed; }
SSH_CAP=$(cap 600) rootssh 'set -e; rm -rf /home/admin/qdistro-test; cp -a /root/qdistro-src /home/admin/qdistro-test;
    chown -R admin: /home/admin/qdistro-test' || setup_failed

# ---- pytest, inside the guest ------------------------------------------------
# The guest lists a suite's process tags in <suite>.tags up front and records
# <tag>.rc beside <tag>.xml: the summary counts a suite complete only when
# every listed process finished (pytest exit 0 or 1) and wrote its junit.
cat > "$W/run-pytest.sh" <<'GUEST'
#!/bin/bash
# In the guest, as admin: run one suite; results land in /home/admin/junit.
set -u
name=$1 dir=$2 args=$3 deadline=$4
J=/home/admin/junit
mkdir -p "$J"
cd "/home/admin/qdistro-test/$dir" || exit 2
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1
run() {  # run TAG ARGS...: at most 30 min, and never past the deadline
    local rc=0 left=$((deadline - $(date +%s)))
    [ "$left" -lt 1800 ] || left=1800
    if [ "$left" -le 0 ]; then echo budget > "$J/$1.rc"; return 1; fi
    timeout -k 30 "$left" dbus-run-session -- python3 -m pytest -p no:cacheprovider -q \
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
    left=$(budget_left)
    if [ "$left" -le 0 ]; then log "budget spent; pytest $name not started"; break; fi
    log "pytest $name..."
    SECONDS=0
    if timeout -k 30 $((left + 120)) ssh "${ssh_opts[@]}" admin@127.0.0.1 \
            "bash run-pytest.sh '$name' '$dir' '$args' $(($(date +%s) + left))" \
            > "$OUT/pytest-$name.log" 2>&1; then rc=0; else rc=$?; fi
    log "pytest $name: $([ "$rc" = 0 ] && echo completed || echo "rc=$rc") (${SECONDS}s)"
    fetch_junit || log "could not fetch the junit results after $name"
done

# ---- bats, from the runner ---------------------------------------------------
if [ "${#BATS_FILES[@]}" -gt 0 ] && [ "$(budget_left)" -le 0 ]; then
    log "budget spent; no bats file started"
    BATS_FILES=()
fi
if [ "${#BATS_FILES[@]}" -gt 0 ]; then
    # What a qci test lane gives its VM on top of the bootstrap chain
    # (fresh-vm-bootstrap.sh): the dev profile file (tier-2 launchers default
    # to the hardened profile without it), the media, multimachine and
    # template installers, the approvals CLI, the probes in a 0755 /root
    # (section 4b) and the RDP certificate of the nested probes (4c).
    SSH_CAP=$(cap 900) rootssh 'set -e; Q=/root/qdistro-src; cd "$Q"
        printf "QDISTRO_PROFILE=dev\n" > /etc/qdistro/profile; chmod 0644 /etc/qdistro/profile
        # The image bakes /home/admin mode 700, but runc init in a rootless
        # keep-id container is already the subuid-mapped container root when
        # it remounts the merged dir MS_PRIVATE — traversal of ~admin then
        # fails EACCES (A/B-verified: o+x fixes it, baseweed test VMs pass
        # through a different launch path). Overlay-only workaround so the
        # container suites test what they test; the product-side question
        # (700 homes vs keep-id) stays open.
        chmod o+x /home/admin
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
        chmod 0600 "$d/rdp.key"; chmod 0644 "$d/rdp.crt"
        # Real qnotebook for the real-app send-to cases (s125 pg15/pg17):
        # the qci lane gets the identical install from fresh-vm-bootstrap.sh;
        # the shipped image, like the kiwi tester, carries no end-user apps.
        # Deps are already present: PyQt6 is an image package, mistune and
        # git-core are TEST_PKGS installed above, qdistro_presentation and
        # qdistro_app are baked chain steps.
        pysite=$(python3 -c "import sysconfig; print(sysconfig.get_paths()[\"purelib\"].replace(\"/usr/lib/\",\"/usr/local/lib/\",1))")
        rm -rf "$pysite/qnotebook"
        install -d -m 0755 "$pysite"
        cp -a "$Q/qnotebook/qnotebook" "$pysite/qnotebook"
        printf "%s\n" "#!/bin/sh" "exec python3 -m qnotebook \"\$@\"" > /usr/local/bin/qnotebook
        chmod 0755 /usr/local/bin/qnotebook' > "$OUT/qci-lane-setup.log" 2>&1 \
        || { tail -30 "$OUT/qci-lane-setup.log"; setup_failed; }

    # The guest is headless: nothing ever feeds input, so qdlocker's
    # production 5-minute idle timer locks the session shortly after it
    # starts, and qdwin then refuses every privileged request (injectFocus,
    # set_keyboard_focus, ...) with `error 3: locked` — fatal to the qdshell
    # binding. qci never sees this because each bats file gets a fresh VM
    # (session younger than the lock) and tiered-isolation's own setup_file
    # installs this same drop-in per-file. Here the session outlives every
    # file, so the suppression must land BEFORE the session exists: qdlocker
    # then starts with the long timeout. Same mechanism as ci/lib/gates/
    # gui.sh's suppress_idle_lock (QDLOCKER_IDLE_MS, 24 h).
    rootssh 'install -d -m0755 /etc/systemd/user/qdlocker.service.d &&
        printf "[Service]\nEnvironment=QDLOCKER_IDLE_MS=86400000\n" \
            > /etc/systemd/user/qdlocker.service.d/99-qci-no-idle-lock.conf' \
        || setup_failed

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
            # A 0xFF starts a fresh reply: whatever came before it on this
            # line is the tail of an abandoned one.
            line = line.rsplit(b"\xff", 1)[-1].strip()
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
# Exit status 0 when the baseline holds (restored or untouched); otherwise
# nonzero, and the loop stops: later files would run on a broken guest.
baseline() {
    rootssh 'sys="greetd qdistro-admin-broker qdistro-session-manager qdistro-pwd qdistro-root-exec.socket"
        usr="qdwin-session.target qdwin-compositor.service qdshell.service"
        healthy() {
            for u in $sys; do systemctl is-active --quiet "$u" || return 1; done
            for u in $usr; do systemctl --user -M admin@ is-active --quiet "$u" || return 1; done
            test -S /run/user/1000/wayland-1
        }
        # Per-file unit forensics BEFORE any recovery below: the next
        # mid-file qdshell death should be self-explaining in this TAP —
        # ActiveState/SubState/Result/NRestarts/ExecMainStatus show whether
        # the shell exited clean (Result=success, which Restart= always
        # now respawns), died on-failure, or hit its start-limit, and
        # whether the compositor restarted underneath it. Additive
        # diagnostics only; recovery logic below is unchanged.
        for u in qdwin-compositor.service qdshell.service; do
            echo "harness: $u state:"
            systemctl --user -M admin@ show "$u" \
                -p ActiveState,SubState,Result,NRestarts,ExecMainStatus 2>&1 || true
        done
        healthy && exit 0
        for u in $sys; do
            systemctl is-active --quiet "$u" && continue
            echo "harness: $u was not active after this file; restarting it"
            systemctl reset-failed "$u" 2>/dev/null; systemctl start "$u" || true
        done
        for u in $usr; do
            systemctl --user -M admin@ is-active --quiet "$u" && continue
            echo "harness: admin $u was not active after this file; starting qdwin-session.target"
            systemctl --user -M admin@ start qdwin-session.target || true
            break
        done
        # One start can lose to units still settling or to a failed member of
        # an already-active target; keep re-issuing start for whatever stays
        # inactive while wayland-1 is missing. Slow emulation needs the time.
        for _ in $(seq 1 45); do
            healthy && exit 0
            for u in $sys; do
                systemctl is-active --quiet "$u" ||
                    { systemctl reset-failed "$u" 2>/dev/null; systemctl start "$u" 2>/dev/null || true; }
            done
            for u in $usr; do
                systemctl --user -M admin@ is-active --quiet "$u" ||
                    { systemctl --user -M admin@ reset-failed "$u" 2>/dev/null;
                      systemctl --user -M admin@ start "$u" 2>/dev/null || true; }
            done
            sleep 2
        done
        # A dead qdshell can leave the compositor shell slot held by a
        # stale wl_client (runs 37102833917 and 37111198372: each new
        # qdshell layer-shell bind is then rejected as not-the-shell-client
        # and the service sits in auto-restart forever). Member restarts
        # cannot break that loop; restarting the compositor drops every
        # wl_client and frees the slot. Do it unconditionally once the poll
        # above exhausted unhealthy — gating on the qdshell is-active raced
        # a respawn window in 37111198372 and skipped the escalation.
        # But only while the admin session target still holds the seat: with
        # the target down the seat fell back to greetd, and a standalone
        # compositor restart can only fail ("no seat", run 37115937113)
        # and burn the start-limit budget the greeter relogin below needs.
        if systemctl --user -M admin@ is-active --quiet qdwin-session.target; then
            echo "harness: session still not healthy; restarting the compositor"
            systemctl --user -M admin@ restart qdwin-compositor.service || true
            for _ in $(seq 1 30); do
                healthy && exit 0
                for u in $usr; do
                    systemctl --user -M admin@ is-active --quiet "$u" ||
                        { systemctl --user -M admin@ reset-failed "$u" 2>/dev/null;
                          systemctl --user -M admin@ start "$u" 2>/dev/null || true; }
                done
                sleep 2
            done
        fi
        echo "harness: baseline NOT restored after this file"
        exit 1' 2>&1
}

for f in "${BATS_FILES[@]}"; do
    base=$(basename "$f" .bats)
    left=$(budget_left)
    if [ "$left" -le 0 ]; then log "budget spent; bats $base and later files not started"; break; fi
    [ "$left" -lt "$BATS_TIMEOUT" ] || left=$BATS_TIMEOUT
    log "bats $base..."
    SECONDS=0
    if (cd "$REPO" && PATH="$W/bin:$PATH" QDSUITES_SOCK="$SOCK" VM_NAME=qdistro-suites \
            env -u QDISTRO_PROFILE -u VM_SSH_PORT -u VM_EXEC \
            timeout -k 30 "$left" bats --tap "$f") \
            > "$OUT/bats/$base.tap" 2>&1; then rc=0; else rc=$?; fi
    echo "# rc=$rc seconds=$SECONDS" >> "$OUT/bats/$base.tap"
    log "bats $base rc=$rc (${SECONDS}s)"
    # Restoring the baseline protects only the NEXT file. After the last
    # file nothing runs again — the guest powers off next — so skip the
    # restore rather than fail a fully-run suite on teardown debris
    # (run 37115937113: shell-modules, deliberately last, left the
    # session seatless and the restore marked the suite incomplete).
    if [ "$f" = "${BATS_FILES[-1]}" ]; then
        log "last bats file; skipping baseline restore"
        break
    fi
    # A file that wedged or rebooted the guest must not poison the rest; the
    # summary lists every file after it as not run.
    if ! SSH_CAP=60 rootssh true 2>/dev/null; then
        echo "$base: the guest stopped answering" > "$OUT/baseline-failed"
        log "guest unreachable after $base; stopping bats"
        break
    fi
    if SSH_CAP=300 baseline > "$W/baseline.out" 2>&1; then brc=0; else brc=$?; fi
    if [ "$brc" -ne 0 ] && vmssh 'pgrep -u _greeter -f qdgreeter >/dev/null' 2>/dev/null; then
        # In-place restarts can lose the seat to the greeter that reappears
        # when admin's session dies; log admin in again, the way the session
        # first came up.
        log "greeter is up after $base; logging admin in again"
        sleep 10   # the password field takes focus once the greeter has painted
        # The new session re-pulls the same units; a start-limit left by the
        # failed restore above would fail the compositor job instantly.
        rootssh 'systemctl --user -M admin@ reset-failed qdwin-session.target qdwin-compositor.service qdshell.service' 2>/dev/null || true
        send_text "$QCI_PASSWORD"
        SECONDS=0
        until session_up; do
            [ "$SECONDS" -lt 150 ] || break
            sleep 2
        done
        if session_up && SSH_CAP=120 baseline >> "$W/baseline.out" 2>&1; then
            echo "harness: admin re-logged at the greeter after this file" >> "$W/baseline.out"
            brc=0
        fi
    fi
    if [ "$brc" -ne 0 ]; then
        SSH_CAP=60 rootssh 'echo "--- failed units"; systemctl --no-pager --failed; \
            echo "--- admin session units"; systemctl --user -M admin@ --no-pager list-units "qdwin*" "qdshell*" 2>&1 | tail -12; \
            echo "--- wayland sockets"; ls -l /run/user/1000/wayland-* 2>&1; \
            echo "--- qdshell journal"; journalctl -b --no-pager _UID=1000 _SYSTEMD_USER_UNIT=qdshell.service -n 15 2>&1; \
            echo "--- compositor journal"; journalctl -b --no-pager _COMM=weston 2>/dev/null | tail -12' \
            >> "$W/baseline.out" 2>&1 || true
    fi
    sed 's/^/# /' "$W/baseline.out" | tee -a "$OUT/bats/$base.tap"
    if [ "$brc" -ne 0 ]; then
        echo "$base: core services or admin's session not restored" > "$OUT/baseline-failed"
        log "core services or admin's session not restored after $base; stopping bats"
        break
    fi
done

SSH_CAP=30 rootssh 'systemctl poweroff' 2>/dev/null || true
for _ in $(seq 1 60); do kill -0 "$qemu_pid" 2>/dev/null || break; sleep 2; done

if summarize; then ended=complete; else
    rc=$?
    [ "$rc" -eq 2 ] || exit 1
    ended=incomplete
fi
cat "$OUT/summary.md"
