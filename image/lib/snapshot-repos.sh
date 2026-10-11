#!/bin/bash
# snapshot-repos.sh — write the Tumbleweed history snapshot zypper repos
# that snapshot.conf pinned (via the source manifest). Sourced by image/config.sh inside the kiwi
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
    local snap repo_dir base
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
    # QDISTRO_REPO_BASE swaps the history base for a local mirror; the layout
    # below it is upstream's (<base>/<snap>/tumbleweed/repo/<name>/). Offline
    # without it we fail closed rather than emit an unreachable upstream URL.
    base="${QDISTRO_REPO_BASE:-}"
    if [ -z "$base" ]; then
        _v_off=
        for _v in "${QDISTRO_DEPS_OFFLINE:-}" "${QCI_OFFLINE:-}"; do
            case "$_v" in 1|true|yes|on) _v_off=1 ;; esac
        done
        if [ -n "$_v_off" ]; then
                echo "snapshot-repos: offline — export QDISTRO_REPO_BASE (host: qd_serve_start + qd_serve_guest_base $snap)" >&2
                return 1
        fi
        base="https://download.opensuse.org/history"
    fi
    mkdir -p "$repo_dir"
    cat > "$repo_dir/qdistro-snapshot-oss.repo" <<EOF
[qdistro-snapshot-oss]
name=qdistro Tumbleweed OSS $snap
enabled=1
autorefresh=0
baseurl=$base/${snap}/tumbleweed/repo/oss/
gpgcheck=1
EOF
    cat > "$repo_dir/qdistro-snapshot-nonoss.repo" <<EOF
[qdistro-snapshot-nonoss]
name=qdistro Tumbleweed NonOSS $snap
enabled=1
autorefresh=0
baseurl=$base/${snap}/tumbleweed/repo/non-oss/
gpgcheck=1
EOF
    chmod 0644 "$repo_dir/qdistro-snapshot-oss.repo" "$repo_dir/qdistro-snapshot-nonoss.repo"
    return 0
}
