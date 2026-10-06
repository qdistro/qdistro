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
# (virsh send-key -> first-differing virsh screenshot, <latency-samples>
# median). Raw guest transcripts land in <logdir>/run-N.log; the lane-style
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
git -C "$repo" archive --format=tar HEAD > "$STAGE/src.tar"
git -C "$repo" rev-parse HEAD > "$STAGE/commit.txt"
cp "$VM_DIR/tier3s-guest-lib.sh" "$VM_DIR/tier3s-guest-setup.sh" "$STAGE/"
cp "$here/bench-guest.sh" "$STAGE/"
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

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
(cd "$STAGE" && exec python3 -m http.server "$PORT" --bind 0.0.0.0 >/dev/null 2>&1) &
HTTP_PID=$!
trap 'kill $HTTP_PID 2>/dev/null' EXIT

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

FAIL=0
echo "== guest-setup (install HEAD + provision runsc + load images)"
ssh_vm "mkdir -p /var/tmp/t3s-dl && cd /var/tmp/t3s-dl && for f in tier3s-guest-lib.sh tier3s-guest-setup.sh commit.txt; do curl -fsS -o \$f http://10.0.2.2:$PORT/\$f || exit 97; done" \
    || { echo "FAIL: fetch"; exit 1; }
# admin GUI session up first (the waypipe bridge needs compositor + qdshell)
ssh_vm 'loginctl enable-linger admin >/dev/null 2>&1; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start qdwin-session.target'
for i in $(seq 1 60); do ssh_vm 'test -S /run/user/1000/wayland-1' && break; sleep 1; done
ssh_vm 'test -S /run/user/1000/wayland-1' || { echo "FAIL: wayland-1 never appeared — VM $VM preserved"; exit 1; }
ssh_vm "cd /var/tmp/t3s-dl && bash tier3s-guest-setup.sh http://10.0.2.2:$PORT --expect-fresh --gui weston-terminal,foot" \
    > "$L/setup.log" 2>&1
if ! grep -q '\[t3s-setup\] [0-9]* passes, 0 failures' "$L/setup.log"; then
    echo "FAIL: guest-setup — see $L/setup.log; VM $VM preserved"; exit 1
fi
ssh_vm "cd /var/tmp/t3s-dl && curl -fsS -o bench-guest.sh http://10.0.2.2:$PORT/bench-guest.sh" || exit 1
echo "   setup PASS ($(grep -o '\[t3s-setup\] [0-9]* passes' "$L/setup.log"))"

for r in $(seq 1 "$RUNS"); do
    echo "== bench pass $r/$RUNS"
    ssh_vm 'cd /var/tmp/t3s-dl && bash bench-guest.sh' > "$L/run-$r.log" 2>&1
    grep -q '\[bench\] [0-9]* passes' "$L/run-$r.log" \
        && echo "   done ($(grep -c '^MEAS' "$L/run-$r.log") measurements)" \
        || { echo "   FAIL (see $L/run-$r.log)"; FAIL=1; }
done

# --- interactive latency: send-key -> first differing screenshot ------------
if [ "$SAMPLES" -gt 0 ]; then
    echo "== interactive latency ($SAMPLES samples: send-key -> frame sha change)"
    if ssh_vm 'cd /var/tmp/t3s-dl && bash bench-guest.sh latency-up' | tee "$L/latency-up.log" | grep -q LATENCY-WINDOW-UP; then
        sleep 3
        shot() { virsh -c qemu:///session screenshot "$VM" "$1" >/dev/null 2>&1 && sha256sum "$1" | cut -d' ' -f1; }
        : > "$L/latency.log"
        for i in $(seq 1 "$SAMPLES"); do
            base=$(shot "$STAGE/f0.ppm")
            t0=$(date +%s%N)
            virsh -c qemu:///session send-key "$VM" KEY_A >/dev/null 2>&1
            for _ in $(seq 1 40); do
                now=$(shot "$STAGE/f1.ppm")
                [ -n "$now" ] && [ "$now" != "$base" ] && break
                sleep 0.2
            done
            t1=$(date +%s%N)
            if [ -n "${now:-}" ] && [ "$now" != "$base" ]; then
                echo "sample_$i $(( (t1 - t0) / 1000000 ))" >> "$L/latency.log"
            else
                echo "sample_$i MISS" >> "$L/latency.log"
            fi
            sleep 0.5
        done
        med=$(awk '$2 ~ /^[0-9]+$/{print $2}' "$L/latency.log" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:int((a[NR/2]+a[NR/2+1])/2)}')
        echo "   median=${med}ms ($(grep -c MISS "$L/latency.log" || true) misses) — includes screenshot-poll granularity (~200-400ms)"
        ssh_vm 'cd /var/tmp/t3s-dl && bash bench-guest.sh latency-down' > /dev/null 2>&1
    else
        echo "   latency window did not come up (see $L/latency-up.log)"
        FAIL=1
    fi
fi

# --- AVC harvest (same contract as run-lanes-enforcing.sh) -------------------
ssh_vm 'if out=$(grep -h "type=AVC" /var/log/audit/audit.log* 2>/dev/null) && [ -n "$out" ]; then printf "%s\n" "$out" | grep "scontext=.*qdistro_tier3s_t" | sort -u; else echo "HARVEST-FAIL: no AVC records collected"; fi; echo "--"; getenforce' \
    > "$L/avc.log" 2>&1
avc_n=$(grep -c denied "$L/avc.log" || true)

{
    echo "# tier3s Phase E bench ($(basename "$L"))"
    echo
    echo "commit=$(cat "$STAGE/commit.txt")  vm=$VM  enforcing=$mode  tier3s-avcs=$avc_n"
    echo "runs=$RUNS samples=$SAMPLES  fail=$FAIL"
    for r in $(seq 1 "$RUNS"); do echo "- run-$r.log"; done
} > "$L/INDEX.md"

if [ "$FAIL" -eq 0 ] && [ "$avc_n" -eq 0 ]; then
    virsh -c qemu:///session destroy "$VM" >/dev/null 2>&1 || true
    virsh -c qemu:///session undefine "$VM" --nvram >/dev/null 2>&1 \
        || virsh -c qemu:///session undefine "$VM" >/dev/null 2>&1 || true
    rm -f "$IMG/${VM}.qcow2"
    echo "== DONE: $L (VM removed)"
else
    echo "== DONE WITH FAILURES: $L — VM $VM preserved (avc=$avc_n fail=$FAIL)"
fi
rm -rf "$STAGE"
