#!/bin/bash
# Shared, explicit input for cloud-derived test VMs. Callers may select a
# different qualified manifest with QDISTRO_TEST_SUBSTRATE=<absolute path>.

qdistro_substrate_snapshot_fresh() {
    local snapshot="$1" today="${2:-$(date -u +%Y%m%d)}" snapshot_epoch today_epoch
    [[ "$snapshot" =~ ^20[0-9]{6}$ && "$today" =~ ^20[0-9]{6}$ ]] || return 1
    snapshot_epoch="$(date -u -d "${snapshot:0:4}-${snapshot:4:2}-${snapshot:6:2}" +%s 2>/dev/null)" || return 1
    today_epoch="$(date -u -d "${today:0:4}-${today:4:2}-${today:6:2}" +%s 2>/dev/null)" || return 1
    [ "$(date -u -d "@$snapshot_epoch" +%Y%m%d)" = "$snapshot" ] || return 1
    [ "$(date -u -d "@$today_epoch" +%Y%m%d)" = "$today" ] || return 1
    [ "$snapshot_epoch" -le "$today_epoch" ] && [ "$(( (today_epoch - snapshot_epoch) / 86400 ))" -le 14 ]
}

qdistro_load_test_substrate() {
    local file="${QDISTRO_TEST_SUBSTRATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/test-substrate.conf}"
    local key value schema='' arch='' cloud_url='' cloud_sha256='' snapshot=''
    [ -f "$file" ] || { echo "ERROR: test substrate manifest missing: $file" >&2; return 1; }
    while IFS='=' read -r key value || [ -n "$key" ]; do
        case "$key" in
            ''|'#'*) continue ;;
            schema|arch|cloud_url|cloud_sha256|snapshot) printf -v "$key" '%s' "$value" ;;
            *) echo "ERROR: unknown test substrate field: $key" >&2; return 1 ;;
        esac
    done < "$file"
    [ "$schema" = 1 ] && [ "$arch" = "$(uname -m)" ] \
        && [[ "$cloud_url" =~ ^https://[^[:space:]]+\.qcow2$ ]] \
        && [[ "$cloud_sha256" =~ ^[0-9a-f]{64}$ ]] \
        && [[ "$snapshot" =~ ^20[0-9]{6}$ ]] || {
        echo "ERROR: invalid test substrate manifest: $file" >&2; return 1;
    }
    qdistro_substrate_snapshot_fresh "$snapshot" || {
        echo "ERROR: Tumbleweed snapshot $snapshot is older than 14 days (or invalid/future); update $file before building or testing" >&2
        return 1
    }
    QDISTRO_SUBSTRATE_FILE="$file"
    QDISTRO_SUBSTRATE_ARCH="$arch"
    QDISTRO_SUBSTRATE_CLOUD_URL="$cloud_url"
    QDISTRO_SUBSTRATE_CLOUD_SHA256="$cloud_sha256"
    QDISTRO_SUBSTRATE_SNAPSHOT="$snapshot"
    export QDISTRO_SUBSTRATE_FILE QDISTRO_SUBSTRATE_ARCH QDISTRO_SUBSTRATE_CLOUD_URL QDISTRO_SUBSTRATE_CLOUD_SHA256 QDISTRO_SUBSTRATE_SNAPSHOT
}

qdistro_substrate_base_path() {
    local kind="$1" img="${2:-${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}}" recipe admin_recipe
    case "$kind" in admin|baked) ;; *) echo "ERROR: unknown baseweed kind: $kind" >&2; return 1 ;; esac
    recipe="$(qdistro_substrate_recipe_digest "$kind")" || return 1
    if [ "$kind" = baked ]; then
        admin_recipe="$(qdistro_substrate_recipe_digest admin)" || return 1
        recipe="$(printf '%s\n%s\n' "$recipe" "$admin_recipe" | sha256sum | awk '{print $1}')"
    fi
    printf '%s/baseweed-%s-%s-%s-%s.qcow2\n' "$img" "$kind" "${QDISTRO_SUBSTRATE_CLOUD_SHA256:0:16}" "$QDISTRO_SUBSTRATE_SNAPSHOT" "${recipe:0:12}"
}

qdistro_substrate_recipe_digest() {
    local kind="$1" vm_dir file digest joined=''
    vm_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    local -a files=("$vm_dir/lib/test-substrate.sh" "$vm_dir/lib/rpm-cache.sh" "$vm_dir/lib/opensuse-cloud-image.sh")
    case "$kind" in
        admin) files+=("$vm_dir/build-baseweed-from-scratch.sh") ;;
        baked) files+=("$vm_dir/build-baked-baseweed.sh" "$vm_dir/install-deps.sh" "$vm_dir/../../tier5-vm/build-guest-image.sh") ;;
        *) echo "ERROR: unknown baseweed recipe: $kind" >&2; return 1 ;;
    esac
    for file in "${files[@]}"; do
        [ -f "$file" ] || { echo "ERROR: recipe input missing: $file" >&2; return 1; }
        digest="$(sha256sum "$file" | awk '{print $1}')" || return 1
        joined+="$digest"$'\n'
    done
    printf '%s' "$joined" | sha256sum | awk '{print $1}'
}

# Guest shell command. Remove cloud-image rolling repos and services before the
# first refresh, then install only the two signed history repositories.
qdistro_substrate_repo_command() {
    local snap="${QDISTRO_SUBSTRATE_SNAPSHOT:?}" digest="${QDISTRO_SUBSTRATE_CLOUD_SHA256:?}"
    cat <<EOF
mkdir -p /etc/zypp/repos.d /etc/qdistro
find /etc/zypp/repos.d -maxdepth 1 -name '*.repo' -delete
find /etc/zypp/services.d -maxdepth 1 -name '*.service' -delete 2>/dev/null || true
printf '[qdistro-snapshot-oss]\\nname=qdistro Tumbleweed OSS $snap\\nenabled=1\\nautorefresh=0\\nkeeppackages=1\\nbaseurl=https://download.opensuse.org/history/$snap/tumbleweed/repo/oss/\\ngpgcheck=1\\n' > /etc/zypp/repos.d/qdistro-snapshot-oss.repo
printf '[qdistro-snapshot-nonoss]\\nname=qdistro Tumbleweed NonOSS $snap\\nenabled=1\\nautorefresh=0\\nkeeppackages=1\\nbaseurl=https://download.opensuse.org/history/$snap/tumbleweed/repo/non-oss/\\ngpgcheck=1\\n' > /etc/zypp/repos.d/qdistro-snapshot-nonoss.repo
printf 'SNAPSHOT=$snap\\nCLOUD_SHA256=$digest\\n' > /etc/qdistro/test-substrate
EOF
}

qdistro_substrate_stamp_ok() {
    local disk="$1" kind="$2" source="$3" recorded_image stamp recipe
    stamp="$disk.substrate"
    [ -s "$disk" ] && [ -s "$stamp" ] || return 1
    recipe="$(qdistro_substrate_recipe_digest "$kind")" || return 1
    grep -Fxq "kind=$kind" "$stamp" \
        && grep -Fxq "cloud_sha256=$QDISTRO_SUBSTRATE_CLOUD_SHA256" "$stamp" \
        && grep -Fxq "snapshot=$QDISTRO_SUBSTRATE_SNAPSHOT" "$stamp" \
        && grep -Fxq "recipe_sha256=$recipe" "$stamp" \
        && grep -Fxq "source_sha256=$source" "$stamp" || return 1
    recorded_image="$(sed -n 's/^image_sha256=//p' "$stamp")"
    [[ "$recorded_image" =~ ^[0-9a-f]{64}$ ]] || return 1
    [ "$(sha256sum "$disk" | awk '{print $1}')" = "$recorded_image" ]
}

qdistro_substrate_write_stamp() {
    local disk="$1" kind="$2" source="$3" tmp recipe
    tmp="$disk.substrate.partial"
    recipe="$(qdistro_substrate_recipe_digest "$kind")" || return 1
    printf 'kind=%s\ncloud_sha256=%s\nsnapshot=%s\nrecipe_sha256=%s\nsource_sha256=%s\nimage_sha256=%s\n' \
        "$kind" "$QDISTRO_SUBSTRATE_CLOUD_SHA256" "$QDISTRO_SUBSTRATE_SNAPSHOT" "$recipe" "$source" \
        "$(sha256sum "$disk" | awk '{print $1}')" > "$tmp"
    mv "$tmp" "$disk.substrate"
}

# Refuse replacement of a fixed base path while any qcow2 in the image dir
# still names it as backing. Unknown/unreadable image metadata also refuses.
qdistro_substrate_replace_safe() {
    local target="$1" dir candidate backing
    dir="$(dirname "$target")"
    [ -e "$target" ] || return 0
    while IFS= read -r -d '' candidate; do
        [ "$candidate" = "$target" ] && continue
        backing="$(qemu-img info -U --output=json "$candidate" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("backing-filename", ""))' 2>/dev/null)" || {
            echo "ERROR: cannot inspect backing of $candidate; refusing to replace $target" >&2; return 1;
        }
        [ -n "$backing" ] || continue
        case "$backing" in /*) ;; *) backing="$(dirname "$candidate")/$backing" ;; esac
        if [ "$(readlink -m "$backing")" = "$(readlink -m "$target")" ]; then
            echo "ERROR: $candidate backs on $target; refusing replacement" >&2
            return 1
        fi
    done < <(find "$dir" -maxdepth 1 -name '*.qcow2' -print0)
}

qdistro_substrate_publish() {
    local partial="$1" dest="$2" kind="$3" source="$4" lock_fd rc=0
    # Clone creation holds this lock shared from overlay creation through
    # domain definition. Publication takes it exclusively and rechecks all
    # existing backing references at the actual replacement boundary.
    exec {lock_fd}>>"$(dirname "$dest")/.qci-storage.lock" || return 1
    flock -x "$lock_fd" || { exec {lock_fd}>&-; return 1; }
    qdistro_substrate_replace_safe "$dest" || rc=1
    if [ "$rc" -eq 0 ]; then
        mv "$partial" "$dest" || rc=1
    fi
    if [ "$rc" -eq 0 ]; then
        chmod 0644 "$dest" || rc=1
        qdistro_substrate_write_stamp "$dest" "$kind" "$source" || rc=1
    fi
    flock -u "$lock_fd"
    exec {lock_fd}>&-
    return "$rc"
}
