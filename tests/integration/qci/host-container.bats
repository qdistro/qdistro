#!/usr/bin/env bats
# qci:host-only
setup() {
    export SOURCE_ROOT="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    export REPO="$BATS_TEST_TMPDIR/repo" QDWIN_CACHE_DIR="$BATS_TEST_TMPDIR/cache"
    mkdir -p "$REPO/ci/bin" "$REPO/ci/containers" "$REPO/scripts/vm/lib" "$REPO/qdterm/util" "$REPO/qdterm/qtermwidget-pyqt" "$BATS_TEST_TMPDIR/bin"
    cp "$SOURCE_ROOT/ci/bin/qci-host-image" "$REPO/ci/bin/"
    cp "$SOURCE_ROOT/ci/containers/"* "$REPO/ci/containers/"
    cp "$SOURCE_ROOT/scripts/vm/container-native-deps.sh" "$REPO/scripts/vm/"
    cp "$SOURCE_ROOT/qdterm/util/build-sip.sh" "$REPO/qdterm/util/"
    echo binding > "$REPO/qdterm/qtermwidget-pyqt/source.sip"
    cat > "$REPO/scripts/vm/lib/test-substrate.sh" <<'SH'
qdistro_load_test_substrate() { QDISTRO_SUBSTRATE_SNAPSHOT=${TEST_SNAPSHOT:-20261003}; QDISTRO_SUBSTRATE_ARCH=x86_64; }
SH
    echo 'qdistro_podman_user_bus() { :; }' > "$REPO/scripts/vm/lib/podman-user-bus.sh"
    git -C "$REPO" init -q
    export CALLS="$BATS_TEST_TMPDIR/calls" IMAGES="$BATS_TEST_TMPDIR/images"
    touch "$CALLS" "$IMAGES"
    cat > "$BATS_TEST_TMPDIR/bin/podman" <<'SH'
#!/bin/bash
printf '%s\n' "$*" >> "$CALLS"
case "$1 $2" in
    'info --format') echo true;;
    'image exists')
        if [[ $3 = registry.* ]]; then [ "${NO_BASE:-0}" = 0 ]; else grep -Fxq "$3" "$IMAGES"; fi;;
    'image inspect')
        if [[ $* = *'.Id'* ]]; then echo "${TEST_BASE_ID:-base-id}"; else echo "${TEST_SNAPSHOT:-20261003}"; fi;;
    'build --pull=never')
        while [ "$#" -gt 0 ]; do
            if [ "$1" = --tag ]; then echo "$2" >> "$IMAGES"; break; fi
            shift
        done;;
    'run --rm') exit "${RUN_RC:-0}";;
    *) echo "unexpected podman call: $*" >&2; exit 99;;
esac
SH
    chmod +x "$BATS_TEST_TMPDIR/bin/podman"
    export PATH="$BATS_TEST_TMPDIR/bin:$PATH"
}

@test "host image cache keys recipe, snapshot and resolved base ID" {
    run "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 0 ]
    run "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 0 ]; [[ "$output" = *'cache hit'* ]]
    [ "$(grep -c '^build ' "$CALLS")" = 1 ]
    echo '# recipe update' >> "$REPO/ci/containers/host-packages.txt"
    run "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 0 ]
    run env TEST_SNAPSHOT=20261004 "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 0 ]
    run env TEST_BASE_ID=other-id "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 0 ]
    [ "$(grep -c '^build ' "$CALLS")" = 4 ]
}

@test "offline refuses missing base and toolchain without pull or build; allows cache hit" {
    run env QCI_OFFLINE=1 NO_BASE=1 "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 3 ]; [[ "$output" = *'missing base'* ]]
    run env QCI_OFFLINE=1 "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 3 ]; [[ "$output" = *'missing host image'* ]]
    ! grep -Eq '^(pull|build) ' "$CALLS"
    run "$REPO/ci/bin/qci-host-image"
    [ "$status" = 0 ]
    run env QCI_OFFLINE=1 "$REPO/ci/bin/qci-host-image"
    echo "$output"; [ "$status" = 0 ]; [[ "$output" = *'cache hit'* ]]
}

@test "launcher only sends row commands to rootless offline Podman and propagates failure" {
    export MARKER="$BATS_TEST_TMPDIR/executed-on-host"
    run bash -c '
        . "$SOURCE_ROOT/ci/lib/host-container.sh"
        QDISTRO_REPO=$REPO
        host_container_run image none touch "$MARKER"
    '
    echo "$output"; [ "$status" = 0 ]
    [ ! -e "$MARKER" ]
    grep -q -- '--userns=keep-id --network=none' "$CALLS"
    grep -q -- 'QT_QPA_PLATFORM=offscreen' "$CALLS"
    run env RUN_RC=42 bash -c '
        . "$SOURCE_ROOT/ci/lib/host-container.sh"
        QDISTRO_REPO=$REPO
        host_container_run image none false
    '
    [ "$status" = 42 ]
}

@test "host row sequence preserves names, kinds and exclusions" {
    run bash -c '
        QDISTRO_REPO=$SOURCE_ROOT WORKSPACE=$SOURCE_ROOT QCI_DIR=$SOURCE_ROOT/ci
        . "$QCI_DIR/lib/bootstrap.sh"
        . "$QCI_DIR/lib/core.sh"
        . "$QCI_DIR/lib/gates/host.sh"
        RDIR=$BATS_TEST_TMPDIR/run; mkdir -p "$RDIR/host"
        run_logged() {
            printf "%s %s %s\n" "$1" "$2" "$4"
            if [ "$2" = qdterm-pytest ]; then
                [[ $6 = *"--ignore=tests/test_print_terminal.py"* ]] || return 30
            fi
        }
        record_skip() { printf "%s %s %s\n" "$1" "$2" "$3"; }
        record_result() { :; }
        coverage_floor_check() { printf "host %s-coverage-floor coverage\n" "$1"; }
        host_container_rows
    '
    echo "$output"; [ "$status" = 0 ]
    expected='host qdistro-ruff lint
host qdistro-mypy lint
host qdistro-pytest pytest
host qdistro-coverage-floor coverage
host qdwin-vendored-libweston-symbols build
host qdwin-vendored-libweston-inert-relptr build
host qdwin-meson build
host qdwin-shell-syntax syntax
host qdshell-local qml
host qdbrowser-pytest pytest
host presentation-pytest pytest
host qdgreeter-pytest pytest
host qdlocker-pytest pytest
host qdfileman-pytest pytest
host qnotebook-pytest pytest
host qdterm-pytest pytest
host qdterm-print-tests pytest
host qdchrome-extension npm
host qdchrome-extension-coverage npm
host qdchrome-extension-coverage-floor coverage
host qdfirefox-extension npm
host qdfirefox-extension-coverage npm
host qdfirefox-extension-coverage-floor coverage'
    [ "$output" = "$expected" ]
}

@test "private row entrypoint refuses to execute on host" {
    run bash "$SOURCE_ROOT/ci/containers/run-host.sh"
    echo "$output"; [ "$status" = 2 ]; [[ "$output" = *'require Podman'* ]]
}

@test "host gate dispatches to the container without executing any row locally" {
    run bash -c '
        . "$SOURCE_ROOT/ci/lib/gates/host.sh"
        EXIT_OK=0
        qci_assert_run_dir() { :; }; qci_assert_repo() { :; }
        gate_edit_guard() { :; }; gate_selftest() { :; }
        run_logged() { echo "unexpected local row"; return 99; }
        host_container_rows() { echo "unexpected local rows"; return 99; }
        host_container_gate() { echo container; return 30; }
        gate_host
    '
    echo "$output"; [ "$status" = 30 ]; [ "$output" = container ]
}

@test "dependency checker clearly fails without Podman and never probes build tools" {
    run bash -c '
        command() {
            [ "$1 $2" != "-v podman" ] || return 1
            case "$2" in bash|git|python3|bats|virsh) return 0;; esac
            echo "unexpected dependency: $*"; return 99
        }
        set -- --check
        . "$SOURCE_ROOT/ci/bin/qci-host-deps"
    '
    echo "$output"; [ "$status" = 1 ]
    [[ "$output" = *"MISSING orchestration prerequisite: podman"* ]]
    [[ "$output" != *"unexpected dependency"* ]]
}
