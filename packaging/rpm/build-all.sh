#!/usr/bin/env bash
# Build all qdistro RPMs in the podman builder image and assemble the repo.
# Usage: build-all.sh [spec ...]   (default: all, in dependency order)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../env.sh
. "$here/../env.sh"
builder=$QDISTRO_RPM_BUILDER

bash "$here/make-sources.sh" "$(cd "$here/../.." && pwd)"

# qdwin first (qdistro-protocols.pc), then the vendored libweston tree and
# the shell that build against it; leaf packages last, metapackage at the end.
order=(qdwin qdistro-libweston-vendored qdistro-daemons qdshell
       qdistro-presentation qdgreeter qdlocker qterminator qfileman
       qnotebook qdbrowser qdistro-session qdistro-admin qdistro-browser-bridge qdistro-desktop)
if [ $# -gt 0 ]; then order=("$@"); fi

podman run --rm \
    -v "$here/specs:/specs:ro,Z" \
    -v "$here/rpmbuild:/rpmbuild:rw,Z" \
    -v "$here/repo:/repo:rw,Z" \
    "$builder" bash -c '
set -euo pipefail
export HOME=/rpmbuild
mkdir -p /rpmbuild/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
for spec in '"${order[*]@Q}"'; do
    echo "=== building $spec ==="
    rpmbuild --define "_topdir /rpmbuild" -ba "/specs/$spec.spec"
    # make locally built rpms visible to later builds (qdwin -> daemons)
    createrepo_c --quiet /repo 2>/dev/null || true
    find /rpmbuild/RPMS -name "*.rpm" -exec cp {} /repo/ \;
    createrepo_c --quiet /repo
    zypper -n --gpg-auto-import-keys addrepo --refresh --no-gpgcheck file:///repo qdistro-local 2>/dev/null || zypper -n refresh qdistro-local
    zypper -n install -y --no-recommends --allow-unsigned-rpm /repo/"$spec"-*.rpm 2>/dev/null \
        || zypper -n install -y --no-recommends --allow-unsigned-rpm --repo qdistro-local "$spec" || true
done
createrepo_c --quiet /repo
echo "=== repo contents ==="
ls -la /repo/*.rpm 2>/dev/null || true
'
