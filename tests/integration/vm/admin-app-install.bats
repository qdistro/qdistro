#!/usr/bin/env bats
# Host-only proof of the admin UI's installed layout and production launcher.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    INSTALLER="$REPO/scripts/install/install-admin-app-for-vm.sh"
    ROOT="$BATS_TEST_TMPDIR/root"
}

@test "admin-app installer stages the app, Wayland launcher, and discoverable desktop entry" {
    run env DESTDIR="$ROOT" bash "$INSTALLER" "$REPO/admin_app"
    [ "$status" -eq 0 ]
    [[ "$output" == *"graphical admin approval UI installed"* ]]

    local app="$ROOT/usr/local/bin/qdistro-admin-approval-app"
    local launcher="$ROOT/usr/local/bin/qdistro-start-admin-app"
    local desktop="$ROOT/usr/share/applications/qdistro-admin-app.desktop"
    cmp "$REPO/admin_app/qdistro_admin_app.py" "$app"
    cmp "$REPO/deploy/start-admin-app-wayland.sh" "$launcher"
    cmp "$REPO/admin_app/qdistro-admin-app.desktop" "$desktop"
    [ "$(stat -c %a "$app")" = 755 ]
    [ "$(stat -c %a "$launcher")" = 755 ]
    [ "$(stat -c %a "$desktop")" = 644 ]
    grep -qx 'Exec=/usr/local/bin/qdistro-start-admin-app' "$desktop"
    grep -qx 'TryExec=/usr/local/bin/qdistro-start-admin-app' "$desktop"
    grep -qx 'export QT_QPA_PLATFORM=wayland' "$launcher"
    grep -qx 'exec /usr/bin/python3 /usr/local/bin/qdistro-admin-approval-app "$@"' "$launcher"
    ! grep -Eq 'QT_QPA_PLATFORM=xcb|/home/admin/qdistro|/root/qdistro-src' "$launcher" "$desktop"
}

@test "admin-app installer refuses a missing UI source before staging files" {
    mkdir -p "$BATS_TEST_TMPDIR/empty/admin_app"
    run env DESTDIR="$ROOT" bash "$INSTALLER" "$BATS_TEST_TMPDIR/empty/admin_app"
    [ "$status" -eq 2 ]
    [[ "$output" == *"missing admin app source"* ]]
    [ ! -e "$ROOT/usr/local/bin/qdistro-admin-approval-app" ]
}

@test "production launcher rejects root and a missing Wayland session" {
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cat > "$BATS_TEST_TMPDIR/bin/id" <<'EOF'
#!/bin/bash
printf '%s\n' "$FAKE_UID"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/id"
    local launcher="$REPO/deploy/start-admin-app-wayland.sh"
    run env PATH="$BATS_TEST_TMPDIR/bin:$PATH" FAKE_UID=0 bash "$launcher"
    [ "$status" -eq 1 ]
    [[ "$output" == *"uid 1000"* ]]

    run env PATH="$BATS_TEST_TMPDIR/bin:$PATH" FAKE_UID=1000 \
        XDG_RUNTIME_DIR="$BATS_TEST_TMPDIR/no-session" WAYLAND_DISPLAY=wayland-test \
        bash "$launcher"
    [ "$status" -eq 1 ]
    [[ "$output" == *"no Wayland display"* ]]
}

@test "production launcher reaches installed app with native Wayland and forwards arguments" {
    mkdir -p "$BATS_TEST_TMPDIR/bin" "$BATS_TEST_TMPDIR/runtime"
    cat > "$BATS_TEST_TMPDIR/bin/id" <<'EOF'
#!/bin/bash
printf '1000\n'
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/id"
    local socket="$BATS_TEST_TMPDIR/runtime/wayland-test"
    local launcher="$REPO/deploy/start-admin-app-wayland.sh"

    # The sandbox denies socket bind, so override only the -S predicate for
    # the expected path, and exec only in this subprocess. The launcher still
    # reaches its final command and reports its environment and argv.
    run env PATH="$BATS_TEST_TMPDIR/bin:$PATH" XDG_RUNTIME_DIR="$BATS_TEST_TMPDIR/runtime" \
        WAYLAND_DISPLAY=wayland-test QT_QPA_PLATFORM=xcb TEST_SOCKET="$socket" \
        bash -c 'function [ { if [[ "$1" == ! && "$2" == -S && "$3" == "$TEST_SOCKET" ]]; then return 1; else builtin [ "$@"; fi; }; export -f "["; exec() { printf "MOCK_EXEC=%s|%s|%s|%s|%s\n" "$QT_QPA_PLATFORM" "$WAYLAND_DISPLAY" "$1" "$2" "$3"; }; export -f exec; bash "$1" approval-test' _ "$launcher"
    [ "$status" -eq 0 ]
    [ "$output" = 'MOCK_EXEC=wayland|wayland-test|/usr/bin/python3|/usr/local/bin/qdistro-admin-approval-app|approval-test' ]

    run env PATH="$BATS_TEST_TMPDIR/bin:$PATH" XDG_RUNTIME_DIR="$BATS_TEST_TMPDIR/runtime" \
        WAYLAND_DISPLAY="$socket" QT_QPA_PLATFORM=xcb TEST_SOCKET="$socket" \
        bash -c 'function [ { if [[ "$1" == ! && "$2" == -S && "$3" == "$TEST_SOCKET" ]]; then return 1; else builtin [ "$@"; fi; }; export -f "["; exec() { printf "MOCK_EXEC=%s|%s|%s|%s|%s\n" "$QT_QPA_PLATFORM" "$WAYLAND_DISPLAY" "$1" "$2" "$3"; }; export -f exec; bash "$1" approval-test' _ "$launcher"
    [ "$status" -eq 0 ]
    [ "$output" = "MOCK_EXEC=wayland|$socket|/usr/bin/python3|/usr/local/bin/qdistro-admin-approval-app|approval-test" ]
}
