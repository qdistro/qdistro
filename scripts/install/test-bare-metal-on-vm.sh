#!/usr/bin/env bash
# test-bare-metal-on-vm.sh — exercise bare-metal-install.sh inside a
# fresh libvirt/virt-manager VM. Builds a one-shot disposable VM from
# an upstream cloud image, copies the installer + sibling repos in,
# runs it noninteractively, and reports pass/fail.
#
# This is the "did the bare-metal installer regress?" gate — before
# you change bare-metal-install.sh, run this against both target
# distros:
#
#   ./test-bare-metal-on-vm.sh tumbleweed
#   ./test-bare-metal-on-vm.sh ubuntu
#   ./test-bare-metal-on-vm.sh both
#
# Flags:
#   --keep                 leave the VM running after success
#   --graphics=none|spice  display backend (default: none = headless,
#                          uses qemu-guest-agent for verification.
#                          Pass --graphics=spice if you'll follow up
#                          with ui-agent-test.sh which needs
#                          'virsh screenshot' to work.)
#
# Requirements on the host:
#   - libvirt + qemu + virt-install + virt-customize (libguestfs)
#   - The user can talk to qemu:///session (no root needed for the
#     session URI; matches scripts/vm/* convention)
#   - Sibling checkouts of qdwin and qdshell next to this qdistro repo
#     (the installer can clone, but bundling them avoids a git fetch
#     each test run)
#
# Lifecycle: VM is named qdistro-baremetal-test-<distro>. Existing VM
# of that name is destroyed + undefined first. Pass --keep to leave
# the VM running for post-mortem.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
QDISTRO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
REPO_PARENT=$(dirname "$QDISTRO_DIR")
INSTALLER="$SCRIPT_DIR/bare-metal-install.sh"

VM_PASSWORD="${QDISTRO_VM_PASSWORD:-test123}"
ADMIN_PW="${BAREMETAL_ADMIN_PW:-adminpass}"
USER_NAME="${BAREMETAL_USER_NAME:-alice}"
USER_PW="${BAREMETAL_USER_PW:-userpass}"
KEEP=0
GRAPHICS="${BAREMETAL_GRAPHICS:-none}"   # 'none' = headless (default,
                                         # works with vm-exec); 'spice'
                                         # = needed for ui-agent-test.sh
                                         # screenshot driving.
DISTROS=()

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        tumbleweed|ubuntu) DISTROS+=("$1") ;;
        both)              DISTROS+=(tumbleweed ubuntu) ;;
        --keep)            KEEP=1 ;;
        --graphics=*)      GRAPHICS="${1#*=}" ;;
        --graphics)        shift; GRAPHICS="$1" ;;
        -h|--help)         usage; exit 0 ;;
        *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
    esac
    shift
done
[ ${#DISTROS[@]} -gt 0 ] || { usage; exit 2; }

[ -x "$INSTALLER" ] || { echo "missing installer: $INSTALLER" >&2; exit 2; }

IMG_DIR="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
CACHE_DIR="$HOME/.cache/qdistro"
install -d "$IMG_DIR" "$CACHE_DIR"

VIRSH="virsh -c qemu:///session"

log()  { printf '\033[1;36m[test-vm]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[test-vm] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------
# Cloud-image URLs
# ----------------------------------------------------------------------
url_for() {
    case "$1" in
        tumbleweed) echo "https://download.opensuse.org/tumbleweed/appliances/openSUSE-Tumbleweed-Minimal-VM.x86_64-Cloud.qcow2" ;;
        ubuntu)     echo "https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img" ;;
        *)          echo "" ;;
    esac
}

prep_image() {
    local distro="$1"
    local url; url=$(url_for "$distro")
    # Cache filename matches build-baseweed-from-scratch.sh's so we
    # share the same cached download.
    local cache
    case "$distro" in
        tumbleweed) cache="$CACHE_DIR/tumbleweed-cloud-base.qcow2" ;;
        ubuntu)     cache="$CACHE_DIR/ubuntu-26.04-cloud-base.qcow2" ;;
    esac
    local dest="$IMG_DIR/qdistro-baremetal-test-$distro.qcow2"

    if [ ! -s "$cache" ]; then
        log "[$distro] downloading cloud image..."
        wget -q --show-progress -O "$cache.partial" "$url"
        mv "$cache.partial" "$cache"
    else
        log "[$distro] reusing cached image: $cache ($(du -h "$cache" | cut -f1))"
    fi

    log "[$distro] cloning + resizing image to 30 GB..."
    rm -f "$dest"
    qemu-img create -f qcow2 "$dest" 30G >/dev/null
    # Match build-baseweed-from-scratch.sh: try --expand of the
    # canonical root part first, fall back to auto-expand if the layout
    # differs (e.g. single-root images).
    if ! virt-resize --quiet --expand /dev/sda3 "$cache" "$dest" >/dev/null 2>&1; then
        rm -f "$dest"
        qemu-img create -f qcow2 "$dest" 30G >/dev/null
        virt-resize --quiet --expand /dev/sda1 "$cache" "$dest" >/dev/null 2>&1 \
            || { rm -f "$dest"; qemu-img create -f qcow2 "$dest" 30G >/dev/null
                 virt-resize --quiet "$cache" "$dest" >&2; }
    fi

    log "[$distro] customizing: root pw + qga + ssh..."
    local pkg_install
    case "$distro" in
        tumbleweed) pkg_install='zypper -n --no-gpg-checks install qemu-guest-agent openssh' ;;
        ubuntu)     pkg_install='apt-get update && apt-get install -y qemu-guest-agent openssh-server' ;;
    esac
    # Mirror build-baseweed-from-scratch.sh: SELinux permissive (so qga
    # can write everywhere during the bootstrap), serial-getty for
    # console debugging, jeos-firstboot masked.
    virt-customize -a "$dest" \
        --memsize 2048 --smp 2 \
        --root-password "password:${VM_PASSWORD}" \
        --run-command "$pkg_install" \
        --run-command 'systemctl enable qemu-guest-agent.service' \
        --run-command 'mkdir -p /etc/systemd/system/qemu-guest-agent.service.d && printf "[Service]\nUser=root\nGroup=root\nDynamicUser=no\nProtectHome=no\nProtectSystem=no\nPrivateTmp=no\n" > /etc/systemd/system/qemu-guest-agent.service.d/run-as-root.conf' \
        --run-command 'mkdir -p /etc/sysconfig && echo "GA_PATH=" > /etc/sysconfig/qemu-ga; echo "GA_USER=root" >> /etc/sysconfig/qemu-ga' \
        --run-command 'systemctl enable ssh.service sshd.service 2>/dev/null || true' \
        --run-command 'systemctl enable serial-getty@ttyS0.service 2>/dev/null || true' \
        --run-command 'systemctl mask cloud-init.service cloud-init-local.service cloud-config.service cloud-final.service cloud-init.target 2>/dev/null || true' \
        --run-command 'systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true' \
        --run-command 'sed -i "s/^SELINUX=.*/SELINUX=permissive/" /etc/selinux/config 2>/dev/null || true' \
        --hostname "qdistro-baremetal-test-$distro" >&2

    # ONLY the disk path on stdout — caller does disk=$(prep_image ...).
    printf '%s\n' "$dest"
}

ensure_destroyed() {
    local name="$1"
    $VIRSH destroy   "$name" 2>/dev/null || true
    $VIRSH undefine  "$name" --remove-all-storage 2>/dev/null || true
}

start_vm() {
    local distro="$1" disk="$2"
    local name="qdistro-baremetal-test-$distro"
    local osv
    case "$distro" in
        tumbleweed) osv=opensusetumbleweed ;;
        ubuntu)     osv=ubuntu24.04 ;;  # 26.04 unknown to libosinfo; closest match
    esac
    ensure_destroyed "$name"
    log "[$distro] booting VM..."
    virt-install \
        --connect qemu:///session \
        --name "$name" \
        --memory 4096 --vcpus 4 \
        --disk path="$disk",format=qcow2,bus=virtio \
        --os-variant "$osv" \
        --network user \
        --graphics "$GRAPHICS" \
        --import \
        --noautoconsole \
        --channel unix,target_type=virtio,name=org.qemu.guest_agent.0 \
        >/dev/null
    # Wait for guest agent.
    for i in $(seq 1 60); do
        if "$SCRIPT_DIR/../vm/vm-exec" "$name" "true" >/dev/null 2>&1; then
            log "[$distro] guest agent up after ${i}s"
            return 0
        fi
        sleep 1
    done
    fail "[$distro] guest agent never came up"
}

stage_sources_in_vm() {
    local distro="$1" name="qdistro-baremetal-test-$distro"
    local stage; stage=$(mktemp -d)
    log "[$distro] tarring sibling repos..."
    tar czf "$stage/qdistro.tar.gz" -C "$REPO_PARENT" qdistro
    [ -d "$REPO_PARENT/qdwin"   ] && tar czf "$stage/qdwin.tar.gz"   -C "$REPO_PARENT" qdwin
    [ -d "$REPO_PARENT/qdshell" ] && tar czf "$stage/qdshell.tar.gz" -C "$REPO_PARENT" qdshell

    # Serve via host:port forwarded over user-mode net (10.0.2.2 = host).
    local port=$(( 18800 + RANDOM % 1000 ))
    (cd "$stage" && python3 -m http.server "$port" --bind 127.0.0.1 \
        >/dev/null 2>&1) &
    local http_pid=$!
    trap "kill $http_pid 2>/dev/null || true; rm -rf '$stage'" RETURN

    log "[$distro] downloading repos inside VM..."
    "$SCRIPT_DIR/../vm/vm-exec" "$name" "mkdir -p /opt/qdistro-src && cd /opt/qdistro-src && for r in qdistro qdwin qdshell; do mkdir -p \$r; wget -q -O - http://10.0.2.2:$port/\$r.tar.gz 2>/dev/null | tar xz -C \$r --strip-components=1 || true; done"
}

run_installer_in_vm() {
    local distro="$1" name="qdistro-baremetal-test-$distro"
    log "[$distro] running bare-metal-install.sh in VM..."
    local cmd
    cmd="cd /opt/qdistro-src/qdistro && bash scripts/install/bare-metal-install.sh"
    cmd+=" --admin-password='$ADMIN_PW'"
    cmd+=" --user='$USER_NAME' --user-password='$USER_PW'"
    cmd+=" --repo-root=/opt/qdistro-src --yes --noninteractive"
    cmd+=" 2>&1 | tee /var/log/qdistro-install.log"
    "$SCRIPT_DIR/../vm/vm-exec" "$name" "$cmd"
}

verify_vm() {
    local distro="$1" name="qdistro-baremetal-test-$distro"
    log "[$distro] verifying install..."
    local checks=(
        "id admin"
        "id $USER_NAME"
        "test -x /usr/lib64/weston/qdwin-shell.so || test -x /usr/lib/weston/qdwin-shell.so"
        "test -f /etc/systemd/system/qdistro-admin-broker.service"
        "systemctl is-enabled qdistro-admin-broker.service"
        "test -f /home/admin/.config/systemd/user/noctalia-shell.service"
    )
    local failed=0
    for c in "${checks[@]}"; do
        if "$SCRIPT_DIR/../vm/vm-exec" "$name" "$c" >/dev/null 2>&1; then
            log "  OK   $c"
        else
            log "  FAIL $c"
            failed=1
        fi
    done
    [ $failed -eq 0 ] || fail "[$distro] verification failed (see /var/log/qdistro-install.log inside the VM)"
    log "[$distro] all checks passed"
}

run_one() {
    local distro="$1"
    log "===== $distro ====="
    local disk; disk=$(prep_image "$distro")
    start_vm   "$distro" "$disk"
    stage_sources_in_vm "$distro"
    run_installer_in_vm "$distro"
    verify_vm  "$distro"
    if [ "$KEEP" -eq 0 ]; then
        ensure_destroyed "qdistro-baremetal-test-$distro"
        log "[$distro] VM destroyed (pass --keep to preserve)"
    else
        log "[$distro] VM kept as 'qdistro-baremetal-test-$distro'"
    fi
}

for d in "${DISTROS[@]}"; do run_one "$d"; done

log "ALL TESTS PASSED"
