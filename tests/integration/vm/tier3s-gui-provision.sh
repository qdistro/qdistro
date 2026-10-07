#!/bin/bash
# tier3s-gui-provision.sh — HOST side. Provision the tier-3s (gVisor runsc)
# stack into an already-running qci GUI VM so the agent-driven GUI scenarios
# under tests/integration/permissions-gui/ (58-tier3s-*) can exercise the
# real end-user path: a sandboxed GUI application reaching the qdwin desktop
# through the waypipe bridge.
#
#   tier3s-gui-provision.sh <vm> [gui-workloads]
#
# <gui-workloads> is a comma list of GUI workload names whose OCI archives
# must be loaded into admin's store (default: weston-terminal — the workload
# with a pinned seccomp profile that the s123 lane launches).
#
# The VM is expected to be a qci GUI worker (dev profile, admin's qdwin
# session available). The script is idempotent: when the tier-3s install
# marker already exists the guest setup still re-runs (the installer is
# idempotent) but WITHOUT --expect-fresh, so re-provisioning a VM that
# already has the stack is supported and the pinned inputs are re-validated.
#
# What it does (mirroring tier3s/bench/run-bench.sh's staging recipe and
# tests/integration/vm/tier3s.bash's t3s_setup_file):
#   1. stage a private dir: src.tar (git archive HEAD), commit.txt,
#      gvisor.tar.zstd (pinned, sha512-checked against tier3s/RUNSC_RELEASE),
#      the per-workload OCI archives + manifest (cache dir keyed by the
#      tested commit's image inputs), and the guest lib/setup scripts;
#   2. serve it over a private python http.server at the guest's
#      default-route gateway (slirp 10.0.2.2 or the passt gateway);
#   3. bring up admin's qdwin session (linger + qdwin-session.target, wait
#      for the wayland-1 socket), stop the idle locker so the scenario's
#      frames are not blanked;
#   4. run tier3s-guest-setup.sh <base-url> --gui <workloads> in the guest —
#      installer with QDISTRO_TIER3S=1, offline runsc provision (sha512),
#      image load (sha256 + image ID against the manifest), broker allow
#      rule, Model A canary.
#
# Missing pinned inputs are a loud failure (build them once with
# tier3s/cache-image-archive.sh <dev-vm>), never a silent skip. The staging
# server and dir are removed on exit; the guest keeps everything it loaded.
set -uo pipefail

VM=${1:?usage: tier3s-gui-provision.sh <vm> [gui-workloads]}
GUI_WL=${2:-weston-terminal}

# virsh/vm-exec resolve the session socket via XDG_RUNTIME_DIR; a bare
# invocation lands on the ~/.cache fallback daemon, which shares the config
# dir but tracks no running domains (and here was spawned with a capped
# RLIMIT_FSIZE — every qemu exec it attempts fails). Pin the session bus.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///session}"

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VMEXEC=${QDISTRO_VM_EXEC:-$repo/scripts/vm/vm-exec}

fail() { echo "[tier3s-gui-provision] FAIL: $*" >&2; exit 1; }
note() { echo "[tier3s-gui-provision] $*"; }

[ -d "$repo/tier3s" ] || fail "repo root has no tier3s/ (got $repo)"
dirty=$(git -C "$repo" status --porcelain -- tier3s session_manager broker templates scripts/install tests/integration/vm/tier3s-guest-lib.sh tests/integration/vm/tier3s-guest-setup.sh)
[ -z "$dirty" ] || fail "uncommitted changes under installed trees; the guest installs git archive HEAD: $dirty"

# --- stage dir -------------------------------------------------------------
STAGE=$(mktemp -d /tmp/t3s-gui-stage.XXXXXX)
HTTP_PID=
cleanup() {
    [ -n "$HTTP_PID" ] && kill "$HTTP_PID" 2>/dev/null
    rm -rf "$STAGE"
}
trap cleanup EXIT

git -C "$repo" archive --format=tar HEAD > "$STAGE/src.tar" || fail "git archive HEAD"
git -C "$repo" rev-parse HEAD > "$STAGE/commit.txt"
cp "$here/tier3s-guest-lib.sh" "$here/tier3s-guest-setup.sh" "$STAGE/" || fail "staging guest scripts"

# Deliver the GUI waiter library the same way the gui gate does
# (ci/lib/gates/gui.sh): /tmp/qci-gui-waiters.sh, sourceable by guest scripts.
WAITERS="$repo/ci/lib/guest/gui-waiters.sh"
[ -f "$WAITERS" ] || fail "missing $WAITERS"
wb64=$(base64 -w0 "$WAITERS")
"$VMEXEC" "$VM" "printf '%s' '$wb64' | base64 -d > /tmp/qci-gui-waiters.sh && bash -n /tmp/qci-gui-waiters.sh" \
    || fail "could not deliver the GUI waiter library to /tmp/qci-gui-waiters.sh"

rel=$(sed -n 's/^release=//p' "$repo/tier3s/RUNSC_RELEASE")
want=$(sed -n 's/^tarball_sha512=//p' "$repo/tier3s/RUNSC_RELEASE")
tar="$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd"
got=$(sha512sum "$tar" 2>/dev/null | cut -d' ' -f1)
[ -n "$want" ] && [ "$got" = "$want" ] \
    || fail "pinned runsc tarball missing or sha512 mismatch: $tar (want $want, got ${got:-none})"
ln -sf "$tar" "$STAGE/gvisor.tar.zstd"

cdir=$(cd "$repo" && bash tier3s/cache-image-archive.sh --dir) || fail "cache-image-archive.sh --dir"
for arch in tier3s-headless-smoke ${GUI_WL//,/ }; do
    arch="tier3s-${arch#tier3s-}.oci.tar"
    [ -s "$cdir/$arch" ] || fail "missing $cdir/$arch (build once: tier3s/cache-image-archive.sh <dev-vm>)"
    ln -sf "$cdir/$arch" "$STAGE/$arch"
done
ln -sf "$cdir/manifest.txt" "$STAGE/image-manifest.txt"

# --- guest gateway + staging server ----------------------------------------
HOST_IP=$("$VMEXEC" "$VM" 'ip route | awk "/^default/ {print \$3; exit}"' 2>/dev/null | tr -d '[:space:]')
[ -n "$HOST_IP" ] || HOST_IP=10.0.2.2
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
BIND=$(ip -o -4 addr show | awk -v ip="$HOST_IP" '$4 ~ "^"ip"/" {print ip; exit}')
(cd "$STAGE" && exec python3 -m http.server "$PORT" --bind "${BIND:-0.0.0.0}" >/dev/null 2>&1) &
HTTP_PID=$!
sleep 0.5; kill -0 "$HTTP_PID" || fail "staging http server did not start on port $PORT"
U="http://$HOST_IP:$PORT"
note "staging server $U for VM $VM"

# --- guest: fetch + session + setup ----------------------------------------
fresharg=
"$VMEXEC" "$VM" 'test -e /usr/lib/qdistro/tier3s/spawn-tier3s.sh' >/dev/null 2>&1 || fresharg=--expect-fresh

# fetch the guest scripts (base64-wrap the guest command: vm-exec JSON-encodes
# its argv by string concatenation, embedded quotes break the parse)
B64=$(base64 -w0 <<EOF
set -e
mkdir -p /var/tmp/t3s-dl
cd /var/tmp/t3s-dl
for f in tier3s-guest-lib.sh tier3s-guest-setup.sh commit.txt; do
    curl -fsS -o "\$f" "$U/\$f"
done
EOF
)
"$VMEXEC" "$VM" "echo $B64 | base64 -d | bash" || fail "guest could not fetch the guest scripts from $U"

# spawn-tier3s.sh refuses any non-dev profile read from /etc/qdistro/profile.
# The baked test substrates are dev images but the stamp file is written by
# the bootstrap path a plain clone never ran — write it when absent, refuse
# to pretend a present non-dev file is dev.
B64=$(base64 -w0 <<'EOF'
if [ -f /etc/qdistro/profile ]; then
    grep -qx 'QDISTRO_PROFILE=dev' /etc/qdistro/profile || exit 3
else
    printf 'QDISTRO_PROFILE=dev\n' > /etc/qdistro/profile; chmod 0644 /etc/qdistro/profile
fi
EOF
)
"$VMEXEC" "$VM" "echo $B64 | base64 -d | bash" || fail "/etc/qdistro/profile present and not dev — tier-3s is dev-profile only"

# admin's GUI session must be live before guest-setup asserts it: the waypipe
# bridge needs the compositor socket and qdshell. Already-up targets are a
# no-op; stop the idle locker so long scenarios keep visible frames.
B64=$(base64 -w0 <<'EOF'
loginctl enable-linger admin >/dev/null 2>&1 || :
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user start qdwin-session.target
for i in $(seq 1 60); do test -S /run/user/1000/wayland-1 && exit 0; sleep 1; done
exit 1
EOF
)
"$VMEXEC" "$VM" "echo $B64 | base64 -d | bash" \
    || fail "admin qdwin session did not come up (no /run/user/1000/wayland-1)"
"$VMEXEC" "$VM" 'runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user stop qdlocker.service' \
    >/dev/null 2>&1 || note "WARN: could not stop qdlocker — long scenarios may idle-lock"

B64=$(base64 -w0 <<EOF
cd /var/tmp/t3s-dl && bash tier3s-guest-setup.sh "$U" $fresharg --gui "$GUI_WL"
EOF
)
"$VMEXEC" "$VM" "echo $B64 | base64 -d | bash > /var/tmp/t3s-dl/gui-provision.log 2>&1" \
    || { "$VMEXEC" "$VM" 'tail -30 /var/tmp/t3s-dl/gui-provision.log' >&2; fail "tier3s-guest-setup.sh failed — guest log at /var/tmp/t3s-dl/gui-provision.log"; }
"$VMEXEC" "$VM" 'tail -3 /var/tmp/t3s-dl/gui-provision.log'
note "PASS: tier-3s provisioned on $VM (workloads: $GUI_WL)"
