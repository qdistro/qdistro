#!/usr/bin/env bats
# Static-invariant lock-in for scripts/vm/spin-test-vm-gui.sh.
#
# Unlike the rest of tests/integration/vm/*.bats, this file does NOT need
# a live VM (no `load helpers`, no VM_NAME). It asserts — by inspecting
# the script SOURCE — that the permissions-gui GUI bootstrap installs
# every helper path, user, and config file the permissions-gui scenarios
# (and tests/integration/permissions-gui/AGENTS.md) assume exist.
#
# Modelled on the s40-tier-hardening static-invariant style: catch a
# regression where someone deletes/renames an install line during a
# refactor and silently breaks scenario provisioning, WITHOUT paying the
# cost of a full VM boot. The companion live-VM check is the
# "--- post-install state ---" block the script itself prints at the end
# of its postboot run.
#
# Run: bats tests/integration/vm/spin-test-vm-gui-bootstrap.bats

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    GUI_SPIN="$REPO_ROOT/scripts/vm/spin-test-vm-gui.sh"
    TEMPLATE="$REPO_ROOT/scripts/vm/create-template-domain.sh"
    AGENTS="$REPO_ROOT/tests/integration/permissions-gui/AGENTS.md"
    [ -f "$GUI_SPIN" ] || {
        echo "spin-test-vm-gui.sh not found at $GUI_SPIN" >&2
        return 1
    }
}

# --- helper: assert a literal substring appears in a file --------------
in_file() {
    local needle="$1" file="$2"
    grep -qF -- "$needle" "$file"
}

@test "gui-spin: script is syntactically valid bash" {
    run bash -n "$GUI_SPIN"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gui-spin: create-template-domain.sh is syntactically valid bash" {
    run bash -n "$TEMPLATE"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}

@test "gui-spin: creates work (uid 2000) and work2 (uid 3000) users" {
    in_file "useradd -m -u 2000" "$GUI_SPIN"
    in_file "useradd -m -u 3000" "$GUI_SPIN"
}

@test "gui-spin: enables linger for both work users" {
    in_file "loginctl enable-linger work" "$GUI_SPIN"
    in_file "loginctl enable-linger work2" "$GUI_SPIN"
}

@test "gui-spin: installs /usr/local/bin/qdistro-test-permission" {
    in_file "/usr/local/bin/qdistro-test-permission" "$GUI_SPIN"
}

@test "gui-spin: installs qdistro_app SDK for qdistro-test-permission" {
    in_file "sysconfig.get_paths()['purelib']" "$GUI_SPIN"
    in_file '$PY_SITE/qdistro_app' "$GUI_SPIN"
    in_file '$SRC/sdk/qdistro_app' "$GUI_SPIN"
    in_file "import qdistro_app" "$GUI_SPIN"
}

@test "gui-spin: installs qdistro-start-admin-app launcher" {
    in_file "/usr/local/bin/qdistro-start-admin-app" "$GUI_SPIN"
}

@test "gui-spin: installs qdistro-start-admin-tui launcher" {
    in_file "/usr/local/bin/qdistro-start-admin-tui" "$GUI_SPIN"
}

@test "gui-spin: installs the qdistro-admin-tui wrapper (scenario 35)" {
    # The 1-liner that was the headline 'still open' item: without it,
    # qterminal falls back to /bin/sh and scenario 35 ERRORs.
    in_file "/usr/local/bin/qdistro-admin-tui" "$GUI_SPIN"
    # ...and it must actually exec the TUI module, not just touch a path.
    in_file "qdistro_admin_tui.py" "$GUI_SPIN"
}

@test "gui-spin: deploys the PyQt admin app onto admin's home" {
    in_file "/home/admin/qdistro/admin_app/qdistro_admin_app.py" "$GUI_SPIN"
}

@test "gui-spin: deploys the Textual TUI sources onto admin's home" {
    in_file "/home/admin/qdistro/tui/" "$GUI_SPIN"
    in_file "qdistro_admin_tui.py" "$GUI_SPIN"
    in_file "broker_client.py" "$GUI_SPIN"
    in_file "silo_colors.py" "$GUI_SPIN"
}

@test "gui-spin: installs qterminal + qterminal.ini pinned geometry" {
    in_file "qterminal" "$GUI_SPIN"
    in_file "/home/admin/.config/qterminal.org/qterminal.ini" "$GUI_SPIN"
    in_file "size=@Size(1200 700)" "$GUI_SPIN"
}

@test "gui-spin: installs the labwc + XWayland + fonts stack" {
    in_file "labwc" "$GUI_SPIN"
    in_file "xwayland" "$GUI_SPIN"
    in_file "dejavu-fonts" "$GUI_SPIN"
}

@test "gui-spin: installs perl-Net-DBus" {
    in_file "perl-Net-DBus" "$GUI_SPIN"
}

@test "gui-spin: configures autologin (greetd masked, agetty on tty1)" {
    in_file "systemctl mask greetd.service" "$GUI_SPIN"
    in_file "getty@tty1.service.d/autologin.conf" "$GUI_SPIN"
    in_file "--autologin admin" "$GUI_SPIN"
}

# --- display-resolution fix (item 2) -----------------------------------

@test "gui-spin: forces virtio_gpu into the initramfs (dracut conf)" {
    in_file "/etc/dracut.conf.d/90-qdistro-virtio-gpu.conf" "$GUI_SPIN"
    in_file "force_drivers+=" "$GUI_SPIN"
    in_file "virtio_gpu" "$GUI_SPIN"
}

@test "gui-spin: blacklists simpledrm on the kernel cmdline" {
    in_file "initcall_blacklist=simpledrm_platform_driver_init" "$GUI_SPIN"
}

@test "gui-spin: regenerates initramfs and bootloader after the fix" {
    in_file "dracut --force --regenerate-all" "$GUI_SPIN"
    in_file "grub2-mkconfig" "$GUI_SPIN"
}

@test "template: default video model is shim-free virtio-gpu-pci" {
    # model='none' suppresses libvirt's default VGA; the GPU is added
    # via the qemu:commandline override as a bare virtio-gpu-pci.
    in_file "virtio-gpu-pci" "$TEMPLATE"
    in_file "<video><model type='none'/></video>" "$TEMPLATE"
    in_file "xmlns:qemu" "$TEMPLATE"
}

@test "template: legacy VGA escape hatch is preserved" {
    in_file "QDISTRO_TEMPLATE_LEGACY_VGA" "$TEMPLATE"
    in_file "<model type='virtio' heads='1' primary='yes'/>" "$TEMPLATE"
}

# --- AGENTS.md ground-truth + input contracts (items 3 + 4) ------------

@test "AGENTS.md: blesses keyboard nav (virsh send-key) as input path" {
    [ -f "$AGENTS" ] || { echo "AGENTS.md missing at $AGENTS" >&2; return 1; }
    in_file "virsh send-key" "$AGENTS"
    in_file "BLESSED input" "$AGENTS"
    in_file "PLATFORM-BLOCKED" "$AGENTS"
}

@test "AGENTS.md: codifies broker-state + stdout as ground truth" {
    in_file "Ground truth" "$AGENTS"
    in_file "GetPending" "$AGENTS"
    in_file "approvals.sqlite" "$AGENTS"
    in_file "the broker state wins" "$AGENTS"
}
