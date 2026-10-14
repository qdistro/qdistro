#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# Static-invariant lock-in for scripts/vm/spin-test-vm-gui.sh.
#
# Unlike the rest of tests/integration/vm/*.bats, this file does NOT need
# a live VM (no `load helpers`, no VM_NAME). It asserts — by inspecting
# the script SOURCE — that the GUI golden bootstrap installs every helper
# path and config the GUI scenarios (and
# tests/integration/permissions-gui/AGENTS.md) assume exist, on the one
# (qdwin) profile, and that it leaves the INSTALLED admin TUI alone.
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
    SILO_FIXTURES="$REPO_ROOT/scripts/vm/lib/gui-silo-fixtures.sh"
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

@test "gui-spin: template provides a guest sound card for live capture tests" {
    run grep -Fq "<sound model='ich9'><audio id='1'/></sound>" "$TEMPLATE"
    [ "$status" -eq 0 ]

    # Existing hosts keep their already-defined template domain.  Clone-time
    # reconciliation ensures those workers gain the sound card too.
    run grep -Fq "if ! grep -q '<sound '" "$REPO_ROOT/scripts/vm/clone-baseweed.sh"
    [ "$status" -eq 0 ]
}

@test "gui-spin: work fixtures are durable active silos, provisioned per scenario" {
    # The session manager owns the Linux-account creation.  A bare useradd
    # creates no silos.yaml row, so the first reconcile purges the relay grant
    # required by the cross-user GUI scenarios. The golden does not bake them
    # (Active silos change the qdlocker lock screen); the scenarios that need
    # them run the same helper at Setup through qdwin_admin_lane_setup --silos.
    in_file 'bash /root/qdistro-src/scripts/vm/lib/gui-silo-fixtures.sh' \
        "$REPO_ROOT/ci/lib/guest/gui-waiters.sh"
    in_file "sm_call CreateSilo" "$SILO_FIXTURES"
    in_file '"work:2000" "work2:3000"' "$SILO_FIXTURES"
    in_file "sm_call StartSilo" "$SILO_FIXTURES"
    in_file "silo_matches" "$SILO_FIXTURES"
    in_file "silo_state" "$SILO_FIXTURES"
    run grep -F 'gui-silo-fixtures.sh' "$GUI_SPIN"
    [ "$status" -ne 0 ] || [[ "$output" != *'bash "$SRC/scripts/vm/lib/gui-silo-fixtures.sh"'* ]]
}

@test "gui-spin: enables linger for both work users" {
    in_file "loginctl enable-linger work" "$SILO_FIXTURES"
    in_file "loginctl enable-linger work2" "$SILO_FIXTURES"
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

@test "gui-spin: installs the admin app as the image does (shipped Wayland launcher)" {
    in_file 'bash "$SRC/scripts/install/install-admin-app-for-vm.sh" "$SRC/admin_app"' "$GUI_SPIN"
    in_file "grep -qx 'export QT_QPA_PLATFORM=wayland' /usr/local/bin/qdistro-start-admin-app" "$GUI_SPIN"
    in_file "/usr/local/bin/qdistro-admin-approval-app" "$GUI_SPIN"
}

@test "gui-spin: never overwrites the installed admin TUI or deploys dev copies" {
    # The retired step 4 ran `cat > /usr/local/bin/qdistro-admin-tui`, which
    # FOLLOWS the installer's symlink and replaced the root-owned module with a
    # wrapper around an admin-writable /home/admin copy: the TUI then never
    # had the broker's authority shape on any GUI golden.
    run grep -nE '(>|install[^#]*|cp[^#]*|ln[^#]*)[[:space:]]*/usr/local/bin/qdistro-admin-tui([[:space:]]|$)' "$GUI_SPIN"
    [ "$status" -ne 0 ] || { echo "writes the TUI entry point: $output" >&2; return 1; }
    run grep -n '/home/admin/qdistro' "$GUI_SPIN"
    [ "$status" -ne 0 ] || { echo "deploys dev copies: $output" >&2; return 1; }
    # It VERIFIES the installed TUI instead, on the bake and on every clone.
    in_file 'verify_installed_tui() {' "$GUI_SPIN"
    in_file '[ "$(readlink /usr/local/bin/qdistro-admin-tui)" = "$lib" ]' "$GUI_SPIN"
    in_file "'#!/usr/bin/python3 -I'" "$GUI_SPIN"
    in_file "/usr/bin/python3 -I -c 'import textual, rich'" "$GUI_SPIN"
    [ "$(grep -c '^ *verify_installed_tui$' "$GUI_SPIN")" -ge 2 ]
}

@test "gui-spin: one profile, the product qdwin session" {
    run grep -nE 'labwc|lxqt|qterminal|xdotool|QDISTRO_VM_GUI_SESSION|start-admin-tui|start-user-app' "$GUI_SPIN"
    [ "$status" -ne 0 ] || { echo "$output" >&2; return 1; }
    in_file "systemctl --user start qdwin-session.target" "$GUI_SPIN"
    in_file "wayland-1 socket up" "$GUI_SPIN"
    in_file "qdwin clone session up (wayland-1)" "$GUI_SPIN"
    in_file "systemctl mask greetd.service" "$GUI_SPIN"
    in_file "renderer=pixman" "$GUI_SPIN"
    in_file "mode=1280x800@60" "$GUI_SPIN"
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

@test "AGENTS.md: blesses keyboard nav and reviewed pointer input paths" {
    [ -f "$AGENTS" ] || { echo "AGENTS.md missing at $AGENTS" >&2; return 1; }
    in_file "virsh send-key" "$AGENTS"
    in_file "BLESSED input" "$AGENTS"
    in_file "click-preview" "$AGENTS"
    in_file "click-confirm" "$AGENTS"
    in_file "Every model-targeted mouse click" "$AGENTS"
}

@test "AGENTS.md: codifies broker-state + stdout as ground truth" {
    in_file "Ground truth" "$AGENTS"
    in_file "GetPending" "$AGENTS"
    in_file "approvals.sqlite" "$AGENTS"
    in_file "the broker state wins" "$AGENTS"
}
