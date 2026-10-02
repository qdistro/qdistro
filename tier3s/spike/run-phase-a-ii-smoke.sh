#!/bin/bash
# tier3s/spike/run-phase-a-ii-smoke.sh — HOST driver for the milestone A-ii VM
# smoke (guest script phase-a-ii-smoke.sh). Serves `git archive HEAD`, the
# sha512-checked runsc tarball and the expected sha256/mode of every tier3s
# artifact the installer places (computed from HEAD, CONTRACT.md §1) on
# 127.0.0.1 (the guest's 10.0.2.2). Every podman/runsc step runs inside the VM.
#   run-phase-a-ii-smoke.sh <vm> <logdir>     (refuses a non-empty logdir)
set -u
vm=${1:?usage: run-phase-a-ii-smoke.sh <vm> <logdir>}
out=${2:?usage: run-phase-a-ii-smoke.sh <vm> <logdir>}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
port=${T3S_PORT:-42961}
if [ -d "$out" ] && [ -n "$(ls -A "$out")" ]; then echo "refusing: $out is not empty" >&2; exit 2; fi
mkdir -p "$out"
[ -z "$(git -C "$repo" status --porcelain -- tier3s session_manager broker scripts/install)" ] \
    || { echo "refusing: uncommitted changes under the installed trees (the VM gets git archive HEAD)" >&2; exit 2; }
pin=$repo/tier3s/RUNSC_RELEASE
rel=$(sed -n 's/^release=//p' "$pin")
want=$(sed -n 's/^tarball_sha512=//p' "$pin")
tarball=$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd
got=$(sha512sum < "$tarball" | cut -d' ' -f1)
[ "$got" = "$want" ] || { echo "host tarball sha512 != pin" >&2; exit 2; }
serve=$(mktemp -d /var/tmp/t3s-serve.XXXXXX)
git -C "$repo" archive --format=tar HEAD > "$serve/src.tar"
git -C "$repo" rev-parse HEAD > "$serve/commit.txt"
cp "$tarball" "$serve/gvisor.tar.zstd"
# expected installed artifacts: <sha256 of the file at HEAD> <mode> <installed path>
h() { git -C "$repo" show "HEAD:$1" | sha256sum | cut -d' ' -f1; }
{
    for f in spawn-tier3s.sh probe.sh tier3s-runsc; do echo "$(h tier3s/$f) 755 /usr/lib/qdistro/tier3s/$f"; done
    echo "$(h tier3s/RUNSC_RELEASE) 644 /usr/lib/qdistro/tier3s/RUNSC_RELEASE"
    for f in $(git -C "$repo" ls-tree --name-only HEAD tier3s/seccomp/ | grep '\.json$'); do
        echo "$(h "$f") 644 /usr/lib/qdistro/tier3s/seccomp/${f##*/}"; done
    for f in qdistro-tier3s-scope qdistro-tier3s-cleanup; do echo "$(h tier3s/$f) 755 /usr/libexec/qdistro/$f"; done
    echo "$(h tier3s/tmpfiles/qdistro-tier3s.conf) 644 /usr/lib/tmpfiles.d/qdistro-tier3s.conf"
    echo "$(h 'session_manager/qdistro-tier3s-silo@.service') 644 /etc/systemd/system/qdistro-tier3s-silo@.service"
    echo "$(h session_manager/qdistro-tier3s-silo-launch) 755 /usr/libexec/qdistro/qdistro-tier3s-silo-launch"
    echo "$(h session_manager/qdistro_session_manager.py) 755 /usr/libexec/qdistro/qdistro_session_manager.py"
    echo "$(h session_manager/org.qdistro.SessionManager1.conf) 644 /etc/dbus-1/system.d/org.qdistro.SessionManager1.conf"
    echo "$(h broker/qdistro_admin_broker.py) 755 /usr/libexec/qdistro/qdistro_admin_broker.py"
} > "$serve/expect.txt"
cp "$serve/expect.txt" "$out/expect.txt"
python3 -m http.server --bind 127.0.0.1 "$port" --directory "$serve" > /dev/null 2>&1 &
srv=$!
trap 'kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null; rm -rf "$serve"; echo "host file server stopped"' EXIT
sleep 1
fails=0
step() {
    local rc
    VMLOG_TIMEOUT=${VMLOG_TIMEOUT:-1800} VMLOG_TAIL=0 "$here/vmlog.sh" "$out/$1.log" "$vm" "$2" > /dev/null
    rc=$?
    printf '%-24s rc=%s %s BAD\n' "$1" "$rc" "$(grep -c ': BAD' "$out/$1.log")"
    [ "$rc" -eq 0 ] || fails=$((fails + 1))
}
U=http://10.0.2.2:$port
G=/root/qdistro-src/tier3s/spike/phase-a-ii-smoke.sh
{ echo "### host: HEAD $(cat "$serve/commit.txt")"; echo "### host: spin commit $(cat /var/tmp/t3s-aii-spin-commit.txt 2>/dev/null || echo '?')"
  echo "### host: tarball sha512 $got (pin $want)"; } > "$out/00-host.log"
# 00: what the spin's bootstrap installer left (before anything here runs), checked
# against HEAD; the tier3s unit has never started.
step 00-stage "set -e; mkdir -p /var/tmp/t3s; curl -fsS $U/expect.txt -o /var/tmp/t3s/expect.txt
rm -rf /root/qdistro-src.aii; mkdir -p /root/qdistro-src.aii; cd /root/qdistro-src.aii
curl -fsS $U/src.tar | tar -xf -; chown -R root:root /root/qdistro-src.aii
echo \"staged commit \$(curl -fsS $U/commit.txt)\"
mkdir -p /var/cache/qdistro/runsc/$rel; curl -fsS $U/gvisor.tar.zstd -o /var/cache/qdistro/runsc/$rel/gvisor.tar.zstd
cat /etc/qdistro/profile; uname -r; podman --version; systemctl --version | head -1; getenforce"
step 01-installed-by-spin "bash /root/qdistro-src.aii/tier3s/spike/phase-a-ii-smoke.sh installed /var/tmp/t3s/expect.txt"
# 02: swap the staged checkout to HEAD and re-run the installer from it
step 02-reinstall "set -e; rm -rf /root/qdistro-src; mv /root/qdistro-src.aii /root/qdistro-src
bash $G reinstall /var/tmp/t3s/expect.txt"
step 03-provision "bash $G provision"
step 04-image "bash $G image"
step 05-rule "bash $G rule"
step 10-smoke-exit "bash $G smoke-exit"
step 11-smoke-live "bash $G smoke-live"
step 12-final "bash $G final"
echo "failed steps: $fails"
exit "$fails"
