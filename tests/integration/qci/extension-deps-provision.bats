#!/usr/bin/env bats
#
# Host-only tests for the extension dependency-provisioning prefix
# (ci/lib/gates/host.sh::host_ext_deps_cmd). No real npm, no network: a PATH
# shim stands in for npm and the REAL snippet the gate builds is executed.
#
# Regression: node_modules/ is gitignored, so every fresh qci run worktree had
# none and `npm test` died "vitest: command not found" (rc 127) in both
# extension rows and, via the missing coverage JSON, both -coverage-floor rows
# (full-20261001T060430Z-757013 and full-20261001T124446Z-1395361).
#
# Contract under test:
#   - toolchain absent  => npm ci runs, then the step proceeds;
#   - toolchain present => npm is NOT invoked (no reinstall per run);
#   - npm ci fails       => nonzero exit + a FAIL: line naming the fix;
#   - npm missing        => nonzero exit + a FAIL: line.

bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/host.sh"
    EXT="$BATS_TEST_TMPDIR/ext"
    SHIM="$BATS_TEST_TMPDIR/bin"
    CALLS="$BATS_TEST_TMPDIR/npm-calls"
    mkdir -p "$EXT" "$SHIM"
    : > "$CALLS"
}

# npm shim: records argv; on `ci` either installs a fake vitest or fails.
mkshim() {
    local mode=$1
    cat > "$SHIM/npm" <<EOF
#!/bin/bash
echo "\$*" >> "$CALLS"
if [ "\$1" = ci ]; then
    [ "$mode" = fail ] && { echo "npm ERR! cache miss" >&2; exit 1; }
    mkdir -p node_modules/.bin && printf '#!/bin/sh\nexit 0\n' > node_modules/.bin/vitest && chmod +x node_modules/.bin/vitest
fi
exit 0
EOF
    chmod +x "$SHIM/npm"
}

run_prefix() {
    local snippet
    snippet="$(host_ext_deps_cmd)echo STEP-REACHED"
    run env PATH="$SHIM:/usr/bin:/bin" bash -c "cd '$EXT' && $snippet"
}

@test "absent node_modules: npm ci --prefer-offline runs, then the step proceeds" {
    mkshim ok
    run_prefix
    [ "$status" -eq 0 ]
    [[ "$output" == *"provision:"* ]]
    [[ "$output" == *"STEP-REACHED"* ]]
    grep -qx 'ci --prefer-offline --no-audit --no-fund' "$CALLS"
    [ -x "$EXT/node_modules/.bin/vitest" ]
}

@test "present node_modules: npm is not invoked" {
    mkshim ok
    mkdir -p "$EXT/node_modules/.bin"
    printf '#!/bin/sh\n' > "$EXT/node_modules/.bin/vitest"
    chmod +x "$EXT/node_modules/.bin/vitest"
    run_prefix
    [ "$status" -eq 0 ]
    [[ "$output" == *"STEP-REACHED"* ]]
    [ ! -s "$CALLS" ]
}

@test "npm ci failure is fatal with a FAIL: line, the step never runs" {
    mkshim fail
    run_prefix
    [ "$status" -ne 0 ]
    [[ "$output" == *"FAIL: npm ci failed"* ]]
    [[ "$output" != *"STEP-REACHED"* ]]
}

@test "npm not installed is fatal with a FAIL: line" {
    run -127 env PATH="$BATS_TEST_TMPDIR/empty" /bin/bash -c "cd '$EXT' && $(host_ext_deps_cmd)echo STEP-REACHED"
    [[ "$output" == *"FAIL: npm not installed"* ]]
    [[ "$output" != *"STEP-REACHED"* ]]
}

@test "both extension rows carry the provisioning prefix" {
    run grep -cE '^[[:space:]]*c="\$\(host_ext_deps_cmd\)\$\(_ext_drift_env qd(firefox|chrome)-extension\)npm test' \
        "$REPO_ROOT/ci/lib/gates/host.sh"
    [ "$status" -eq 0 ]
    [ "$output" -eq 2 ]
}
