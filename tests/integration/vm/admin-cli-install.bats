#!/usr/bin/env bats
# Host-only proof of the non-graphical approval surfaces' installed layout
# (scripts/install/install-admin-cli-for-vm.sh, run by the chain's admin-app
# step and by fresh-vm-bootstrap.sh). The paths are the broker's trusted
# control-plane paths, so they are asserted exactly.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    INSTALLER="$REPO/scripts/install/install-admin-cli-for-vm.sh"
    ROOT="$BATS_TEST_TMPDIR/root"
}

@test "admin-cli installer stages the root CLI and the TUI at the broker-trusted paths" {
    run env DESTDIR="$ROOT" bash "$INSTALLER" "$REPO"
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdistro-approvals"*"qdistro-admin-tui"*"installed"* ]]
    cmp "$REPO/cli/qdistro_approvals.py" "$ROOT/usr/local/sbin/qdistro-approvals"
    [ "$(stat -c %a "$ROOT/usr/local/sbin/qdistro-approvals")" = 755 ]
    head -1 "$ROOT/usr/local/sbin/qdistro-approvals" | grep -qx '#!/usr/bin/python3 -I'
    # the broker's trust lists name these exact paths
    grep -q '"/usr/local/sbin/qdistro-approvals"' "$REPO/broker/qdistro_admin_broker.py"
    grep -q '"/usr/local/bin/qdistro-admin-tui"' "$REPO/broker/qdistro_admin_broker.py"
    [ -L "$ROOT/usr/local/bin/qdistro-admin-tui" ]
    [ "$(readlink "$ROOT/usr/local/bin/qdistro-admin-tui")" = /usr/local/lib/qdistro/admin-tui/qdistro_admin_tui.py ]
    local m
    for m in __init__.py broker_client.py silo_colors.py qdistro_admin_tui.py; do
        cmp "$REPO/tui/$m" "$ROOT/usr/local/lib/qdistro/admin-tui/$m"
    done
    [ "$(stat -c %a "$ROOT/usr/local/lib/qdistro/admin-tui/qdistro_admin_tui.py")" = 755 ]
    [ "$(stat -c %a "$ROOT/usr/local/lib/qdistro/admin-tui/broker_client.py")" = 644 ]
    head -1 "$ROOT/usr/local/lib/qdistro/admin-tui/qdistro_admin_tui.py" | grep -qx '#!/usr/bin/python3 -I'
}

@test "admin-app installer runs the admin-cli installer (one chain step lays down all three surfaces)" {
    run env DESTDIR="$ROOT" bash "$REPO/scripts/install/install-admin-app-for-vm.sh" "$REPO/admin_app"
    [ "$status" -eq 0 ]
    [ -f "$ROOT/usr/local/bin/qdistro-admin-approval-app" ]
    [ -f "$ROOT/usr/local/sbin/qdistro-approvals" ]
    [ -L "$ROOT/usr/local/bin/qdistro-admin-tui" ]
}

@test "admin-cli installer refuses a tree missing a TUI module before staging" {
    mkdir -p "$BATS_TEST_TMPDIR/src/cli" "$BATS_TEST_TMPDIR/src/tui"
    cp "$REPO/cli/qdistro_approvals.py" "$BATS_TEST_TMPDIR/src/cli/"
    run env DESTDIR="$ROOT" bash "$INSTALLER" "$BATS_TEST_TMPDIR/src"
    [ "$status" -eq 2 ]
    [[ "$output" == *"missing admin CLI/TUI source"* ]]
    [ ! -e "$ROOT/usr/local/sbin/qdistro-approvals" ]
}

@test "fresh-vm-bootstrap installs the CLI through the same installer, not a copy" {
    grep -q 'scripts/install/install-admin-cli-for-vm.sh' "$REPO/scripts/vm/fresh-vm-bootstrap.sh"
    ! grep -qE 'install .*cli/qdistro_approvals\.py' "$REPO/scripts/vm/fresh-vm-bootstrap.sh"
}
