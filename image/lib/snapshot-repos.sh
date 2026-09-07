#!/bin/bash
# snapshot-repos.sh — write the Tumbleweed history snapshot zypper repos
# that config.xml pinned. Sourced by image/config.sh inside the kiwi
# chroot and by tests on the host. kiwi OEM does not persist the
# description's <repository> entries into /etc/zypp/repos.d (the dir
# is empty on a packed image), so bootstrap-on-kiwi has nothing to
# refresh. Keep the function body in sync with the copy in
# scripts/vm/fresh-vm-bootstrap.sh (that script runs before $SRC exists).
#
# qdistro_write_snapshot_repos [release-file]
#   Reads SNAPSHOT=YYYYMMDD from /etc/qdistro/release (or the path
#   given). Writes oss + non-oss repo files. FATAL (return 1) on a
#   missing or malformed snapshot: a repo pointing at rolling
#   Tumbleweed would undrift the pin.

qdistro_write_snapshot_repos() {
    local release="${1:-/etc/qdistro/release}"
    local snap repo_dir
    if [ ! -s "$release" ]; then
        echo "snapshot-repos: $release missing or empty" >&2
        return 1
    fi
    snap="$(sed -n 's/^SNAPSHOT=\([0-9]\{8\}\)$/\1/p' "$release" | head -n1)"
    if [ -z "$snap" ]; then
        echo "snapshot-repos: $release has no SNAPSHOT=<YYYYMMDD> line" >&2
        return 1
    fi
    repo_dir="${QDISTRO_ZYPP_REPOS_D:-/etc/zypp/repos.d}"
    mkdir -p "$repo_dir"
    cat > "$repo_dir/qdistro-snapshot-oss.repo" <<EOF
[qdistro-snapshot-oss]
name=qdistro Tumbleweed OSS $snap
enabled=1
autorefresh=0
baseurl=https://download.opensuse.org/history/${snap}/tumbleweed/repo/oss/
gpgcheck=1
EOF
    cat > "$repo_dir/qdistro-snapshot-nonoss.repo" <<EOF
[qdistro-snapshot-nonoss]
name=qdistro Tumbleweed NonOSS $snap
enabled=1
autorefresh=0
baseurl=https://download.opensuse.org/history/${snap}/tumbleweed/repo/non-oss/
gpgcheck=1
EOF
    chmod 0644 "$repo_dir/qdistro-snapshot-oss.repo" "$repo_dir/qdistro-snapshot-nonoss.repo"
    return 0
}
