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

# QDISTRO_REPO_BASE swaps the history base for a local mirror; the layout
# below it is upstream's (<base>/<snapshot>/tumbleweed/repo/<name>/). Offline
# without it we fail closed rather than emit an unreachable upstream URL.
base="${QDISTRO_REPO_BASE:-}"
if [ -z "$base" ]; then
    _v_off=
    for _v in "${QDISTRO_DEPS_OFFLINE:-}" "${QCI_OFFLINE:-}"; do
        case "$_v" in 1|true|yes|on) _v_off=1 ;; esac
    done
    if [ -n "$_v_off" ]; then
        echo "tier2-snapshot-repos: offline — export QDISTRO_REPO_BASE (host: qd_serve_start + qd_serve_guest_base $snapshot)" >&2
        exit 3
    fi
    base="https://download.opensuse.org/history"
fi

cat >"$repo_dir/qdistro-snapshot-oss.repo" <<EOF
[qdistro-snapshot-oss]
name=qdistro Tumbleweed OSS $snapshot
enabled=1
autorefresh=0
baseurl=$base/$snapshot/tumbleweed/repo/oss/
gpgcheck=1
EOF
cat >"$repo_dir/qdistro-snapshot-nonoss.repo" <<EOF
[qdistro-snapshot-nonoss]
name=qdistro Tumbleweed NonOSS $snapshot
enabled=1
autorefresh=0
baseurl=$base/$snapshot/tumbleweed/repo/non-oss/
gpgcheck=1
EOF
chmod 0644 "$repo_dir/qdistro-snapshot-oss.repo" \
    "$repo_dir/qdistro-snapshot-nonoss.repo"
