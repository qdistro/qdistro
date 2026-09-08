#!/bin/sh
# Replace repositories inherited from the rolling Tumbleweed base image with
# the exact history snapshot used to build qdistro.  Container builds call
# this before their first zypper refresh, so metadata and packages cannot come
# from different rolling-repository generations.
set -eu

snapshot_file="${1:-/usr/lib/qdistro/tier2/SNAPSHOT}"
repo_dir="${QDISTRO_ZYPP_REPOS_D:-/etc/zypp/repos.d}"

[ -f "$snapshot_file" ] || {
    echo "tier2-snapshot-repos: missing snapshot file: $snapshot_file" >&2
    exit 2
}
snapshot="$(cat "$snapshot_file")"
case "$snapshot" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
    *)
        echo "tier2-snapshot-repos: expected exactly YYYYMMDD in $snapshot_file" >&2
        exit 2
        ;;
esac

mkdir -p "$repo_dir"
rm -f "$repo_dir"/*.repo

cat >"$repo_dir/qdistro-snapshot-oss.repo" <<EOF
[qdistro-snapshot-oss]
name=qdistro Tumbleweed OSS $snapshot
enabled=1
autorefresh=0
baseurl=https://download.opensuse.org/history/$snapshot/tumbleweed/repo/oss/
gpgcheck=1
EOF
cat >"$repo_dir/qdistro-snapshot-nonoss.repo" <<EOF
[qdistro-snapshot-nonoss]
name=qdistro Tumbleweed NonOSS $snapshot
enabled=1
autorefresh=0
baseurl=https://download.opensuse.org/history/$snapshot/tumbleweed/repo/non-oss/
gpgcheck=1
EOF
chmod 0644 "$repo_dir/qdistro-snapshot-oss.repo" \
    "$repo_dir/qdistro-snapshot-nonoss.repo"
