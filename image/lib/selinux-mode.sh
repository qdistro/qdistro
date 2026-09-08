#!/usr/bin/env bash
# On-disk image configuration only: setting host runtime mode from a Kiwi
# chroot would affect the builder, not the image that is being produced.
qdistro_image_selinux_mode() {
    local config="$1" profile="$2" mode
    case "$profile" in
        dev) mode=permissive ;;
        release) mode=enforcing ;;
        *) echo "invalid image profile: $profile" >&2; return 1 ;;
    esac
    [ -f "$config" ] || return 1
    sed -i '/^[[:space:]]*SELINUX[[:space:]]*=/d' "$config" || return 1
    printf '\nSELINUX=%s\n' "$mode" >> "$config" || return 1
    [ "$(grep -E '^[[:space:]]*SELINUX[[:space:]]*=' "$config")" = "SELINUX=$mode" ]
}
