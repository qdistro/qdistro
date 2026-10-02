#!/bin/bash
# test-vm-guest-install.sh — install qdistro into a runtime-only openSUSE
# Minimal-VM cloud guest. Run as root inside the booted guest by
# .github/workflows/qdistro-test-vm.yml.
#
# Inputs already on the guest disk (copied in offline by the workflow):
#   /                    the native stage from scripts/vm/build-native-podman.sh
#                        (qdwin, daemons, qdshell plugin, vendored libweston,
#                        qsu, SELinux .pp modules), built against snapshot.conf
#   /root/qdistro-src    the monorepo tree at the built commit
#   /etc/zypp/repos.d    only the history snapshot repos of snapshot.conf
#
# Same product content as the kiwi tester image (image/config.sh): the
# bootstrap's own functions, sourced, with qdgreeter on tty3 and the session
# started by the greeter. No compiler, Meson or headers are installed here.
#
# Left out (optional, or needs hardware the test VM does not have):
#   browser-bridge   browser native-messaging bridge (no browser in this VM)
#   phone            cut from v1 (decision D4)
#   print            proxy for the print VM, which needs nested VMs
#   snapshots        btrfs backups; the cloud image root is XFS
#   tier4-host tier5 tier5b
#                    per-app VMs: libvirt/QEMU and nested KVM
# Override with QDISTRO_TEST_VM_SKIP_STEPS (space-separated chain step names).
set -euo pipefail
umask 022

SRC=/root/qdistro-src
# Not ADMIN_PASSWORD: sourcing the bootstrap resets that global to "".
TVM_PASSWORD=${QDISTRO_TEST_VM_PASSWORD:-qdistro}
SKIP_STEPS=${QDISTRO_TEST_VM_SKIP_STEPS:-browser-bridge phone print snapshots tier4-host tier5 tier5b}

tvm_log() { echo "[test-vm] $*"; }
tvm_die() { echo "[test-vm] FATAL: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || tvm_die "must run as root"
[ -f "$SRC/scripts/install/qdistro-bootstrap.sh" ] || tvm_die "source tree missing at $SRC"

# ---- 1. Native stage matches this guest -------------------------------------
# The stage was compiled in a container of the same snapshot; refuse a guest
# whose core libraries differ (same check as fresh-vm-bootstrap.sh).
snap=$(sed -n 's/^SNAPSHOT=//p' /etc/qdistro/test-substrate)
[ "$(cat /usr/share/qdistro-build/snapshot)" = "$snap" ] \
    || tvm_die "native stage snapshot $(cat /usr/share/qdistro-build/snapshot) differs from guest snapshot $snap"

# ---- 2. Runtime packages -----------------------------------------------------
# QDISTRO_RUNTIME_PKGS is the cloud test base's list without compilers and
# headers. Drop what this VM does not run: VM tiers, test-only tools, btrfs
# backup tools and the browser autofill D-Bus library.
# shellcheck source=install-deps.sh
. "$SRC/scripts/vm/install-deps.sh"
pkgs=()
for pkg in "${QDISTRO_RUNTIME_PKGS[@]}"; do
    case "$pkg" in
        libvirt|libvirt-*|virt-install|qemu-*|libguestfs|guestfs-tools) ;;
        tesseract-ocr|ydotool|bats|jq|Mesa-demo-egl|kernel-default) ;;
        rage-encryption|rsync|python313-jeepney) ;;
        *) pkgs+=("$pkg") ;;
    esac
done
# What image/config.xml adds for a bootable desktop: pip for the source-built
# apps, fonts for Qt text, the seat daemon, and the pinned SELinux policy the
# staged modules load into.
pkgs+=(python313-pip dejavu-fonts google-noto-coloremoji-fonts seatd
       selinux-policy-targeted policycoreutils qemu-guest-agent libpango-1_0-0)
tvm_log "installing ${#pkgs[@]} runtime packages from snapshot $snap..."
zypper -n refresh
zypper -n install --no-recommends "${pkgs[@]}"
for tool in meson gcc cc ninja; do
    if command -v "$tool" >/dev/null; then tvm_die "build tool $tool is installed in the runtime guest"; fi
done
while read -r pkg version; do
    [ "$(rpm -q --qf '%{VERSION}-%{RELEASE}' "$pkg" 2>/dev/null)" = "$version" ] \
        || tvm_die "native stage was built against $pkg $version; guest has $(rpm -q "$pkg" 2>&1)"
done < /usr/share/qdistro-build/build-rpms.txt

# ---- 3. Staged ELF closure -----------------------------------------------------
export LD_LIBRARY_PATH=/usr/libexec/qdistro/qdwin-libweston/lib64
n=0
while IFS= read -r f; do
    [ -s "$f" ] || tvm_die "staged ELF missing: $f"
    if ldd "$f" | grep -q 'not found'; then
        ldd "$f" >&2
        tvm_die "unresolved library in $f"
    fi
    n=$((n + 1))
done < /usr/share/qdistro-build/elf-manifest
unset LD_LIBRARY_PATH
tvm_log "checked $n staged ELF files"

# ---- 4. Source-built Python apps ---------------------------------------------
# --no-build-isolation: setuptools comes from the snapshot RPM, not PyPI.
for dir in sdk/presentation qdgreeter qdlocker; do
    tvm_log "pip install $dir..."
    python3 -m pip install --quiet --break-system-packages --no-deps \
        --no-build-isolation --prefix=/usr "$SRC/$dir"
done
# shellcheck source=../../image/lib/pip-app-qml-gate.sh
. "$SRC/image/lib/pip-app-qml-gate.sh"
pip_app_qml_gate qdgreeter qdlocker

# ---- 5. The bootstrap, minus the optional steps -----------------------------
# The qsu and session installers take the staged binaries instead of
# compiling. QDWIN_SESSION_AUTOSTART=0: the greeter starts the session, as in
# the kiwi image.
export QCI_NATIVE_STAGE=1 QSU_PREBUILT_BINARY=/usr/local/bin/qsu
export QDWIN_SESSION_AUTOSTART=0
export QDISTRO_REPO_ROOT="$SRC" QDISTRO_PROFILE=dev QDISTRO_STRICT=1
# shellcheck source=../install/qdistro-bootstrap.sh
. "$SRC/scripts/install/qdistro-bootstrap.sh"
eval "_qdistro_full_chain() $(declare -f installer_chain_entries | tail -n +2)"
installer_chain_entries() {
    local line skip
    while IFS= read -r line; do
        for skip in $SKIP_STEPS; do
            [ "${line%%|*}" = "$skip" ] && continue 2
        done
        printf '%s\n' "$line"
    done < <(_qdistro_full_chain)
}
tvm_log "installer chain: $(installer_chain_names | tr '\n' ' ')"
main --profile=dev --noninteractive --yes \
    --skip-packages --skip-sources --skip-build --reset-passwords \
    --repo-root="$SRC" \
    --admin-password="$TVM_PASSWORD" \
    --user=user --user-password="$TVM_PASSWORD"

# ---- 6. Prebuilt SELinux modules ----------------------------------------------
# The bootstrap's install-policy.sh calls skip without selinux-policy-devel;
# load the modules compiled against this snapshot instead.
for pol in pwd broker session_manager tier1 presentation; do
    semodule -i "/usr/share/qdistro-build/selinux/qdistro_$pol.pp"
done
restorecon -R /usr/libexec/qdistro /var/lib/qdistro 2>/dev/null || true

# ---- 7. qdlocker in the session target ----------------------------------------
# The bootstrap's `systemctl --user enable qdlocker.service` lands this link
# only when admin's user manager answers; write it directly, as
# image/config.sh does, so the greeter-started session always has a locker.
units=/home/admin/.config/systemd/user
install -d -o admin -g "$(id -gn admin)" -m 0755 "$units/qdwin-session.target.wants"
ln -sf ../qdlocker.service "$units/qdwin-session.target.wants/qdlocker.service"
chown -h admin:"$(id -gn admin)" "$units/qdwin-session.target.wants/qdlocker.service"

tvm_log "installed chain steps:"
sed 's/^/[test-vm]   /' /var/lib/qdistro/bootstrap/installer-chain.state
tvm_log "done"
