#!/bin/bash
# Host-side RPM seed/export for cloud-derived baseweed builds. zypper validates
# repository metadata and RPM signatures; these bytes are only download hints.

qdistro_rpm_cache_dir() {
    local root="${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}"
    printf '%s/rpm/%s/%s\n' "$root" "$QDISTRO_SUBSTRATE_SNAPSHOT" "$QDISTRO_SUBSTRATE_ARCH"
}

qdistro_rpm_cache_import() {
    local disk="$1" dir lock_fd rc
    dir="$(qdistro_rpm_cache_dir)"
    [ -d "$dir/packages" ] || return 0
    command -v virt-copy-in >/dev/null || { echo "ERROR: virt-copy-in missing" >&2; return 1; }
    echo "[rpm-cache] seeding $disk from $dir"
    exec {lock_fd}>"$dir/.merge.lock" || return 1
    flock -s "$lock_fd" || { exec {lock_fd}>&-; return 1; }
    virt-copy-in -a "$disk" "$dir/packages" /var/cache/zypp; rc=$?
    flock -u "$lock_fd"
    exec {lock_fd}>&-
    return "$rc"
}

qdistro_rpm_cache_export() {
    local disk="$1" dir tmp
    dir="$(qdistro_rpm_cache_dir)"
    command -v virt-copy-out >/dev/null || { echo "ERROR: virt-copy-out missing" >&2; return 1; }
    command -v rsync >/dev/null || { echo "ERROR: rsync missing" >&2; return 1; }
    mkdir -p "$dir"
    tmp="$(mktemp -d "$dir/export.XXXXXX")" || return 1
    if ! virt-copy-out -a "$disk" /var/cache/zypp/packages "$tmp"; then
        echo "ERROR: could not export RPM cache from $disk" >&2
        rm -r -- "$tmp"
        return 1
    fi
    if ! (
        flock -x 9 || exit 1
        mkdir -p "$dir/packages"
        rsync -a "$tmp/packages/" "$dir/packages/"
    ) 9>"$dir/.merge.lock"; then
        rm -r -- "$tmp"
        return 1
    fi
    rm -r -- "$tmp"
    echo "[rpm-cache] exported packages to $dir"
}
