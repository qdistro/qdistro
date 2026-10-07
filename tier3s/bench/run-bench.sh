#!/bin/bash
# tier3s/bench/run-bench.sh — HOST side Phase E driver.
#
#   run-bench.sh <logdir> [runs] [latency-samples]
#
# Clones ONE fresh enforcing worker (baseweed-enforcing-baked.qcow2, the
# shipping confinement shape), provisions it from `git archive HEAD` exactly
# as the enforcing lanes do (src.tar + pinned runsc + checked OCI archives
# over a private http server), brings up the admin GUI session, then runs
# tier3s/bench/bench-guest.sh <runs> times and the interactive-latency probe
# (virsh send-key -> first pixel change inside the target window's rect,
# verified by ppmdiff.py against baselines taken with no input;
# <latency-samples> median). Raw guest transcripts land in
# <logdir>/run-N.log; the lane-style
# AVC harvest lands in avc.log. The worker is destroyed on success and
# preserved for triage on failure.
set -uo pipefail

L=$1; shift || { echo "usage: $0 <logdir> [runs] [latency-samples]"; exit 2; }
RUNS="${1:-3}"; SAMPLES="${2:-20}"
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VM_DIR="$repo/tests/integration/vm"
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
ENFORCING="$IMG/baseweed-enforcing-baked.qcow2"
KEYFILE="$HOME/.ssh/qdistro_enforcing_id_ed25519"

[ -f "$ENFORCING" ] || { echo "ERROR: $ENFORCING missing" >&2; exit 2; }
[ -f "$KEYFILE" ] || { echo "ERROR: $KEYFILE missing" >&2; exit 2; }
[ -e "$L/INDEX.md" ] && { echo "refusing: $L already has a run"; exit 2; }
mkdir -p "$L"

dirty=$(git -C "$repo" status --porcelain -- tier3s session_manager broker templates scripts/install tests/integration/vm/tier3s-guest-lib.sh tests/integration/vm/tier3s-guest-setup.sh)
[ -z "$dirty" ] || { echo "ERROR: uncommitted changes under installed trees; the worker installs git archive HEAD: $dirty" >&2; exit 2; }

ssh_vm() {
    ssh -p "$SSH_PORT" -i "$KEYFILE" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes \
        root@127.0.0.1 "$@"
}

# --- stage dir: the same inputs the bats t3s_stage serves -------------------
STAGE=$(mktemp -d /tmp/t3s-bench-stage.XXXXXX)
# cleanup must cover early staging failures too — trap before any exit path
HTTP_PID=
trap '[ -n "$HTTP_PID" ] && kill $HTTP_PID 2>/dev/null; rm -rf "$STAGE"' EXIT
git -C "$repo" archive --format=tar HEAD > "$STAGE/src.tar"
git -C "$repo" rev-parse HEAD > "$STAGE/commit.txt"
cp "$VM_DIR/tier3s-guest-lib.sh" "$VM_DIR/tier3s-guest-setup.sh" "$STAGE/"
cp "$here/bench-guest.sh" "$STAGE/"
# the syscall probe: compiled here (no toolchain on the worker); dynamic —
# the exported image rootfs ships glibc + ld-linux, and it also runs bare on
# the guest and bind-mounted into the tier-2 image
gcc -O2 -o "$STAGE/syscost" "$here/syscost.c" \
    || { echo "ERROR: gcc required to build the syscost probe" >&2; exit 2; }
rel=$(sed -n 's/^release=//p' "$repo/tier3s/RUNSC_RELEASE")
want=$(sed -n 's/^tarball_sha512=//p' "$repo/tier3s/RUNSC_RELEASE")
tar="$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd"
got=$(sha512sum "$tar" 2>/dev/null | cut -d' ' -f1)
[ -n "$want" ] && [ "$got" = "$want" ] || { echo "ERROR: runsc cache $tar missing or sha512 != RUNSC_RELEASE" >&2; exit 2; }
ln -sf "$tar" "$STAGE/gvisor.tar.zstd"
cdir=$(cd "$repo" && bash tier3s/cache-image-archive.sh --dir)
for arch in tier3s-headless-smoke tier3s-weston-terminal tier3s-foot; do
    [ -s "$cdir/$arch.oci.tar" ] || { echo "ERROR: missing $cdir/$arch.oci.tar" >&2; exit 2; }
    ln -sf "$cdir/$arch.oci.tar" "$STAGE/$arch.oci.tar"
done
ln -sf "$cdir/manifest.txt" "$STAGE/image-manifest.txt"
python3 - "$cdir" <<'PY' || exit 2
import hashlib, sys, pathlib
cdir = pathlib.Path(sys.argv[1])
man = dict(l.split("=", 1) for l in (cdir / "manifest.txt").read_text().splitlines() if "=" in l)
for name, key in [("tier3s-headless-smoke", "IMAGE_ARCHIVE_SHA256"),
                  ("tier3s-weston-terminal", "IMAGE_ARCHIVE_SHA256_WESTON_TERMINAL"),
                  ("tier3s-foot", "IMAGE_ARCHIVE_SHA256_FOOT")]:
    got = hashlib.sha256((cdir / f"{name}.oci.tar").read_bytes()).hexdigest()
    assert got == man[key], f"{name}: sha256 {got} != manifest {man[key]}"
print("archive sha256s match manifest")
PY

echo "== cloning enforcing worker (commit $(cat "$STAGE/commit.txt" | cut -c1-9))"
out=$("$repo/scripts/vm/clone-baseweed.sh" "t3s-bench" --from-enforcing-baked 2>&1)
VM=$(printf '%s\n' "$out" | sed -n '1p')
SSH_PORT=$(printf '%s\n' "$out" | sed -n '2p' | sed 's/^ssh_port=//')
[ -n "$VM" ] && [ -n "$SSH_PORT" ] || { printf '%s\n' "$out"; exit 1; }
echo "   VM=$VM ssh_port=$SSH_PORT"
for i in $(seq 1 30); do ssh_vm 'true' 2>/dev/null && break; sleep 5; done
mode=$(ssh_vm 'getenforce' 2>/dev/null || echo unknown)
echo "   getenforce=$mode"
[ "$mode" = "Enforcing" ] || { echo "FAIL: worker is $mode, not Enforcing — VM $VM preserved"; exit 1; }

# passt shared-network clones have no slirp 10.0.2.2: the host is the
# guest's default-route gateway (same discovery as vm_host_ip in helpers).
HOST_IP=$(ssh_vm 'ip route | awk "/^default/ {print \$3; exit}"')
[ -n "$HOST_IP" ] || { echo "FAIL: could not discover guest->host gateway"; exit 1; }

# bind the staging server to the guest-facing address only when it is one of
# ours; otherwise 0.0.0.0 (it serves just this staging dir for the run's life)
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
BIND=$(ip -o -4 addr show | awk -v ip="$HOST_IP" '$4 ~ "^"ip"/" {print ip; exit}')
(cd "$STAGE" && exec python3 -m http.server "$PORT" --bind "${BIND:-0.0.0.0}" >/dev/null 2>&1) &
HTTP_PID=$!
sleep 0.5; kill -0 $HTTP_PID || { echo "FAIL: staging http server did not start"; exit 1; }
U="http://$HOST_IP:$PORT"

FAIL=0
echo "== guest-setup (install HEAD + provision runsc + load images)"
ssh_vm "mkdir -p /var/tmp/t3s-dl /var/tmp/t3s-bench && cd /var/tmp/t3s-dl && for f in tier3s-guest-lib.sh tier3s-guest-setup.sh commit.txt; do curl -fsS -o \$f $U/\$f || exit 97; done && curl -fsS -o /var/tmp/t3s-bench/syscost $U/syscost && chmod 755 /var/tmp/t3s-bench/syscost" \
    || { echo "FAIL: fetch"; exit 1; }
# admin GUI session up first (the waypipe bridge needs compositor + qdshell)
ssh_vm 'loginctl enable-linger admin >/dev/null 2>&1; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start qdwin-session.target'
for i in $(seq 1 60); do ssh_vm 'test -S /run/user/1000/wayland-1' && break; sleep 1; done
ssh_vm 'test -S /run/user/1000/wayland-1' || { echo "FAIL: wayland-1 never appeared — VM $VM preserved"; exit 1; }
# the bench runs for minutes with no input: the idle lock would blank the
# session mid-pass and starve the bridge/latency probes of damage. Stop the
# locker on the throwaway worker (ctrl socket has no unlock, by design).
ssh_vm 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop qdlocker.service' \
    || echo "WARN: could not stop qdlocker — GUI sections may idle-lock"
# compositor presentation health: a boot where virtio-gpu atomic commits
# fail EINVAL never reaches scanout — the screen stays on fbcon while the
# wayland socket exists, and the latency desk/win frames are identical.
# Detect a climbing repaint-flush count here, restart the session once,
# and bail (VM preserved) if it keeps failing.
repaint_fails() {
    ssh_vm 'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --no-pager -b" 2>/dev/null | grep -c "repaint-flush failed"'
}
sleep 5; rf1=$(repaint_fails); sleep 4; rf2=$(repaint_fails)
if [ "${rf2:-0}" -gt 5 ] && [ "${rf2:-0}" -gt "${rf1:-0}" ]; then
    echo "   compositor repaint failing ($rf1->$rf2) — restarting the session once"
    ssh_vm 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop qdwin-session.target; sleep 2; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start qdwin-session.target'
    for i in $(seq 1 60); do ssh_vm 'test -S /run/user/1000/wayland-1' && break; sleep 1; done
    sleep 8; rf3=$(repaint_fails); sleep 4; rf4=$(repaint_fails)
    [ "${rf4:-0}" -le "${rf3:-0}" ] \
        || { echo "FAIL: compositor repaint still failing after restart ($rf3->$rf4) — VM $VM preserved"; exit 1; }
    echo "   repaint failures stopped after restart (total $rf4)"
fi
ssh_vm "cd /var/tmp/t3s-dl && bash tier3s-guest-setup.sh $U --expect-fresh --gui weston-terminal,foot" \
    > "$L/setup.log" 2>&1
if ! grep -q '\[t3s-setup\] [0-9]* passes, 0 failures' "$L/setup.log"; then
    echo "FAIL: guest-setup — see $L/setup.log; VM $VM preserved"; exit 1
fi
ssh_vm "cd /var/tmp/t3s-dl && curl -fsS -o bench-guest.sh $U/bench-guest.sh" || exit 1
echo "   setup PASS ($(grep -o '\[t3s-setup\] [0-9]* passes' "$L/setup.log"))"

for r in $(seq 1 "$RUNS"); do
    echo "== bench pass $r/$RUNS"
    ssh_vm 'cd /var/tmp/t3s-dl && bash bench-guest.sh' > "$L/run-$r.log" 2>&1
    grep -q '\[bench\] [0-9]* passes, 0 failures' "$L/run-$r.log" \
        && echo "   done ($(grep -c '^MEAS' "$L/run-$r.log") measurements)" \
        || { echo "   FAIL (see $L/run-$r.log)"; FAIL=1; }
done

# --- interactive latency: send-key -> first differing screenshot ------------
if [ "$SAMPLES" -gt 0 ]; then
    echo "== interactive latency ($SAMPLES samples: send-key -> in-window pixel change)"
    # desktop frame before the window maps -> diff with the windowed frame
    # gives the target window's rect, so a post-key change can be verified
    # to land INSIDE the focused window rather than anywhere on screen
    # screendump can flake on a contended host — retry a few times
    shot() {
        local i
        for i in 1 2 3; do
            virsh -c qemu:///session screenshot "$VM" "$1" >/dev/null 2>&1 \
                && [ -s "$1" ] && return 0
            sleep 1
        done
        return 1
    }
    # settle the desk frame: teardown animations from the last pass must be
    # finished or the desk->win diff (the window rect) picks them up
    for _ in $(seq 1 10); do
        shot "$STAGE/desk.ppm" && sleep 1 && shot "$STAGE/desk2.ppm" \
            && [ "$(python3 "$here/ppmdiff.py" "$STAGE/desk.ppm" "$STAGE/desk2.ppm")" = same ] && break
        sleep 1
    done
    if ssh_vm 'cd /var/tmp/t3s-dl && bash bench-guest.sh latency-up' | tee "$L/latency-up.log" | grep -q LATENCY-WINDOW-UP; then
        sleep 3
        winrect=""
        # localization is mandatory but a single frame can lag/flake —
        # retry the windowed shot; if the rect never resolves the run fails
        for _ in $(seq 1 10); do
            if shot "$STAGE/win.ppm" && [ -s "$STAGE/desk.ppm" ]; then
                # dense-region bbox: the window body — a panel-clock tick or
                # icon repaint in the same frame can't widen the rect
                winrect=$(python3 "$here/ppmdiff.py" "$STAGE/desk.ppm" "$STAGE/win.ppm" rect)
                case "$winrect" in diff*) winrect=${winrect#diff } ;; *) winrect="" ;; esac
            fi
            [ -n "$winrect" ] && break
            sleep 2
        done
        : > "$L/latency.log"
        if [ -z "$winrect" ]; then
            echo "   latency: window rect detection failed — cannot scope the oracle" >&2
            echo "window-rect MISS(setup)" >> "$L/latency.log"; FAIL=1
            # record compositor paint health — a boot with failing atomic
            # commits (virtio-gpu EINVAL) leaves the desk frame unchanged
            ssh_vm 'runuser -l admin -c "journalctl --user -u qdwin-compositor.service --no-pager -b" 2>/dev/null | grep -c "repaint-flush failed"' \
                | sed 's/^/weston-repaint-flush-failures: /' >> "$L/latency.log" || true
        fi
        for i in $(seq 1 "$SAMPLES"); do
            [ -n "$winrect" ] || break
            # ambient-noise control: three baselines spanning ~1.2s. Only a
            # change inside the TARGET WINDOW mask can impersonate a key
            # echo — panel/clock repaints outside it are ignored. Remove
            # the previous sample's frames first so a failed capture can
            # never reuse a stale baseline.
            rm -f "$STAGE/f0.ppm" "$STAGE/f0b.ppm" "$STAGE/f0c.ppm"
            shot "$STAGE/f0.ppm" && sleep 0.6 && shot "$STAGE/f0b.ppm" \
                && sleep 0.6 && shot "$STAGE/f0c.ppm" \
                || { echo "sample_$i MISS(capture)" >> "$L/latency.log"; sleep 0.5; continue; }
            d01=$(python3 "$here/ppmdiff.py" "$STAGE/f0.ppm" "$STAGE/f0b.ppm" inwin $winrect)
            d12=$(python3 "$here/ppmdiff.py" "$STAGE/f0b.ppm" "$STAGE/f0c.ppm" inwin $winrect)
            case "$d01 $d12" in
                *error*|*" error"*)
                    echo "sample_$i MISS(setup)" >> "$L/latency.log"; sleep 0.5; continue;;
            esac
            [ "$d01" = same ] && [ "$d12" = same ] \
                || { echo "sample_$i MISS(noise) [$d01|$d12]" >> "$L/latency.log"; sleep 0.5; continue; }
            t0=$(date +%s%N)
            if ! virsh -c qemu:///session send-key "$VM" KEY_A >/dev/null 2>&1; then
                echo "sample_$i MISS(setup)" >> "$L/latency.log"; sleep 0.5; continue
            fi
            hit=""
            for _ in $(seq 1 40); do
                if shot "$STAGE/f1.ppm"; then
                    d=$(python3 "$here/ppmdiff.py" "$STAGE/f0.ppm" "$STAGE/f1.ppm" inwin $winrect)
                    [ "${d%% *}" = diff ] && { hit=$d; break; }
                fi
                sleep 0.2
            done
            t1=$(date +%s%N)
            if [ -n "$hit" ]; then
                echo "sample_$i $(( (t1 - t0) / 1000000 ))" >> "$L/latency.log"
            else
                echo "sample_$i MISS" >> "$L/latency.log"
            fi
            sleep 0.5
        done
        med=$(awk '$2 ~ /^[0-9]+$/{print $2}' "$L/latency.log" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:int((a[NR/2]+a[NR/2+1])/2)}')
        miss=$(grep -c MISS "$L/latency.log" || true)
        echo "   median=${med}ms ($miss misses) — includes screenshot-poll granularity (~200-400ms)"
        [ "$miss" -eq 0 ] || FAIL=1
        ssh_vm 'cd /var/tmp/t3s-dl && bash bench-guest.sh latency-down' > /dev/null 2>&1
    else
        echo "   latency window did not come up (see $L/latency-up.log)"
        FAIL=1
    fi
fi

# --- AVC harvest (same contract as run-lanes-enforcing.sh) -------------------
# The harvest must PROVE it ran: a failed ssh/harvest yields an empty or
# HARVEST-FAIL log — never let that count as zero denials, and the worker
# must still be Enforcing at the end.
ssh_vm 'if out=$(grep -h "type=AVC" /var/log/audit/audit.log* 2>/dev/null) && [ -n "$out" ]; then printf "%s\n" "$out" | grep "scontext=.*qdistro_tier3s_t" | sort -u; else echo "HARVEST-FAIL: no AVC records collected"; fi; echo "--"; getenforce' \
    > "$L/avc.log" 2>&1 || FAIL=1
avc_n=$(grep -c denied "$L/avc.log" || true)
tail -1 "$L/avc.log" | grep -qx 'Enforcing' \
    || { echo "FAIL: audit harvest incomplete (see $L/avc.log)"; FAIL=1; }
grep -q HARVEST-FAIL "$L/avc.log" && { echo "FAIL: audit harvest empty"; FAIL=1; }

{
    echo "# tier3s Phase E bench ($(basename "$L"))"
    echo
    echo "commit=$(cat "$STAGE/commit.txt")  vm=$VM  enforcing=$mode  tier3s-domain-avcs=$avc_n"
    echo "runs=$RUNS samples=$SAMPLES  fail=$FAIL"
    for r in $(seq 1 "$RUNS"); do echo "- run-$r.log"; done
} > "$L/INDEX.md"

if [ "$FAIL" -eq 0 ] && [ "$avc_n" -eq 0 ]; then
    virsh -c qemu:///session destroy "$VM" >/dev/null 2>&1
    virsh -c qemu:///session undefine "$VM" --nvram >/dev/null 2>&1 \
        || virsh -c qemu:///session undefine "$VM" >/dev/null 2>&1
    # unlink the overlay only when a SUCCESSFUL query proves the domain
    # is gone — a failed `virsh list` (daemon down, conn error) is
    # indeterminate and must preserve the disk
    local_doms=$(virsh -c qemu:///session list --all --name 2>/dev/null); lrc=$?
    if [ "$lrc" -ne 0 ]; then
        echo "WARN: libvirt query failed — VM $VM state unknown, overlay preserved" >&2
        FAIL=1
    elif printf '%s\n' "$local_doms" | grep -qx "$VM"; then
        echo "WARN: VM $VM still defined — preserving, disk kept" >&2
        FAIL=1
    else
        rm -f "$IMG/${VM}.qcow2"
        echo "== DONE: $L (VM removed)"
    fi
fi
[ "$FAIL" -eq 0 ] && [ "$avc_n" -eq 0 ] || {
    echo "== DONE WITH FAILURES: $L — VM $VM preserved (avc=$avc_n fail=$FAIL)"
    exit 1
}
