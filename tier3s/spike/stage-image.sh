#!/bin/bash
# tier3s/spike/stage-image.sh — build the Phase S workload image OFFLINE,
# as root INSIDE the dev test VM. THROWAWAY (Phase S only).
#
# No registry pull and no zypper download: the rootfs is assembled from the
# VM's OWN installed Tumbleweed packages (snapshot 20260929, the snapshot.conf
# pin the VM was built from). The only additions are foot + its three missing
# deps, which come from the host's snapshot-20260929 RPM cache, are checked
# against a SHA256SUMS list AND rpm signature (`rpm -K`) and are installed
# into the VM with rpm (no network). The rootfs = the files of the dependency
# closure of SEEDS (per rpm's own Requires/Provides, scriptlet-only deps
# skipped), then `podman import`ed into admin's rootless store as $IMG.
#
# Usage: stage-image.sh <rpm-dir-with-SHA256SUMS>
set -euo pipefail
. "$(dirname "$0")/lib.sh"
RPMDIR=${1:?usage: stage-image.sh <rpm-dir>}
ROOTFS=$WORK/rootfs
SEEDS="filesystem glibc bash coreutils util-linux iproute2 procps grep sed findutils
python313-base weston waypipe wayland-utils foot dejavu-fonts fontconfig
xkeyboard-config terminfo-base"

say "1. foot set: sha256 + rpm signature, then offline rpm install into the VM"
( cd "$RPMDIR" && sha256sum -c SHA256SUMS )
rpm -K "$RPMDIR"/*.rpm
need=()
for f in "$RPMDIR"/*.rpm; do
    n=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}' "$f")
    rpm -q "$n" >/dev/null 2>&1 || need+=("$f")
done
if [ ${#need[@]} -gt 0 ]; then rpm -Uvh "${need[@]}"; else echo "already installed"; fi
rpm -q foot libfcft4 libutf8proc3 libnanosvg0 libnanosvgrast0 terminfo

say "2. dependency closure of the seeds over the VM rpmdb"
python3 - "$WORK/closure.txt" $SEEDS <<'PY'
import subprocess, sys
out, seeds = sys.argv[1], sys.argv[2:]
def q(*a):
    r = subprocess.run(["rpm", "-q", *a], capture_output=True, text=True)
    return r.returncode, r.stdout
seen, todo, unresolved = set(), list(seeds), []
while todo:
    n = todo.pop()
    if n in seen: continue
    rc, _ = q(n)
    if rc: sys.exit(f"seed/dep package not installed: {n}")
    seen.add(n)
    rc, reqs = q("--qf", "[%{REQUIREFLAGS:deptype}\t%{REQUIRES}\n]", n)
    for line in reqs.splitlines():
        typ, cap = line.split("\t", 1)
        if cap.startswith("rpmlib(") or cap.startswith("("):
            continue
        kinds = set(typ.split(","))
        if kinds and kinds <= {"pre", "post", "preun", "postun", "interp", "pretrans", "posttrans", "verify"}:
            continue
        rc, prov = q("--whatprovides", "--qf", "%{NAME}\n", cap.split(" ")[0])
        if rc: unresolved.append(f"{n}: {cap}"); continue
        for p in sorted(set(prov.split())):
            if p not in seen: todo.append(p)
open(out, "w").write("\n".join(sorted(seen)) + "\n")
print(f"closure: {len(seen)} packages")
for u in unresolved: print("UNRESOLVED (skipped):", u)
PY
xargs rpm -q < "$WORK/closure.txt" > "$WORK/closure-nvra.txt"
wc -l < "$WORK/closure-nvra.txt"

say "3. assemble rootfs from the closure's files (docs/ghosts/man/locale skipped)"
rm -rf "$ROOTFS"; mkdir -p "$ROOTFS"
filelist() {  # $@ = package names
    rpm -q --qf '[%{FILEFLAGS:fflags}\t%{FILENAMES}\n]' "$@" | awk -F'\t' '
        $1 ~ /g/ || $1 ~ /d/ { next }
        $2 ~ "^/usr/share/(man|doc|info|locale)/" { next }
        $2 ~ "^/etc/(passwd|group|shadow|gshadow)$" { next }
        { print substr($2, 2) }' | LC_ALL=C sort -u
}
# filesystem first so /bin,/lib,/sbin land as the usrmerge symlinks.
filelist filesystem > "$WORK/files-filesystem.txt"
# shellcheck disable=SC2046
filelist $(grep -vx filesystem "$WORK/closure.txt") > "$WORK/files-rest.txt"
tar -C / --no-recursion --ignore-failed-read -T "$WORK/files-filesystem.txt" -cf - | tar -C "$ROOTFS" -xpf -
tar -C / --no-recursion --ignore-failed-read -T "$WORK/files-rest.txt" -cf - 2>"$WORK/tar-rest.err" | tar -C "$ROOTFS" -xpf -
echo "tar warnings: $(wc -l < "$WORK/tar-rest.err")"; head -5 "$WORK/tar-rest.err" || true

say "4. minimal identity + caches + banner"
cat > "$ROOTFS/etc/passwd" <<'PW'
root:x:0:0:root:/root:/bin/bash
admin:x:1000:1000:admin:/tmp:/bin/bash
nobody:x:65534:65534:nobody:/var/lib/nobody:/bin/sh
PW
cat > "$ROOTFS/etc/group" <<'GR'
root:x:0:
admin:x:1000:
nobody:x:65534:
GR
cp /etc/os-release "$ROOTFS/etc/os-release" 2>/dev/null || cp /usr/lib/os-release "$ROOTFS/usr/lib/os-release"
install -d -m 0755 "$ROOTFS/usr/local/bin" "$ROOTFS/home/admin"
cat > "$ROOTFS/usr/local/bin/t3s-banner" <<'BN'
#!/bin/bash
# Printed at the top of the terminal running inside the tier3s sandbox.
echo "== tier3s Phase S: this shell runs under gVisor (runsc) =="
echo "kernel: $(cat /proc/version)"
dmesg --syslog 2>&1 | head -3
id
echo "WAYLAND_SOCKET=${WAYLAND_SOCKET:-unset} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset}"
exec /bin/bash --norc -i
BN
chmod 0755 "$ROOTFS/usr/local/bin/t3s-banner"
ldconfig -r "$ROOTFS"
chroot "$ROOTFS" /usr/bin/fc-cache -s -f >/dev/null 2>&1 && echo "fc-cache ok" || echo "fc-cache FAILED"
du -sh "$ROOTFS"

say "5. import into admin's rootless store as $IMG"
tar -C "$ROOTFS" --numeric-owner -cf "$WORK/rootfs.tar" .
sha256sum "$WORK/rootfs.tar"
chmod 0644 "$WORK/rootfs.tar"
as_admin podman rmi -f "$IMG" >/dev/null 2>&1 || true
as_admin podman import --change 'USER 1000:1000' --change 'WORKDIR /tmp' \
    --change 'ENV LANG=C.UTF-8' "$WORK/rootfs.tar" "$IMG"
as_admin podman image inspect --format '{{.Id}} {{.Size}} {{.Config.User}} {{.Config.Env}}' "$IMG"
rm -f "$WORK/rootfs.tar"
echo "STAGE-IMAGE DONE"
