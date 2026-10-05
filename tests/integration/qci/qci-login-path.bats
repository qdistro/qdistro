#!/usr/bin/env bats
# qci_login_cmd: steps run under `bash -lc`, and a login shell sources
# /etc/profile, which on openSUSE rebuilds PATH unless PROFILEREAD is set (it
# is unset under `systemd-run --user`). The caller's PATH must still come first.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    STUB="$(mktemp -d "${BATS_TMPDIR:-/tmp}/qci login path.XXXXXX")"
    mkdir -p "$STUB/it's bin"
    printf '#!/bin/sh\necho stub\n' > "$STUB/it's bin/qci-login-stub"
    chmod +x "$STUB/it's bin/qci-login-stub"
}

teardown() {
    rm -rf "$STUB"
}

@test "login shell without PROFILEREAD keeps the caller's PATH first" {
    PATH="$STUB/it's bin:$PATH"
    run env -u PROFILEREAD bash -lc "$(qci_login_cmd 'command -v qci-login-stub; qci-login-stub')"
    [ "$status" -eq 0 ]
    [ "${lines[0]}" = "$STUB/it's bin/qci-login-stub" ]
    [ "${lines[1]}" = stub ]
}

@test "the wrapped command's own status and arguments pass through" {
    run env -u PROFILEREAD bash -lc "$(qci_login_cmd 'printf "%s|" "a b" "$HOME" >/dev/null; exit 7')"
    [ "$status" -eq 7 ]
}
