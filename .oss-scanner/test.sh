#!/usr/bin/env bash
# all is the headless audit lane. VM/GUI/enforcing-SELinux checks are separate.
set -uo pipefail
cd "$(dirname "$0")/.." || exit
if [ "${OSS_SCANNER_TEST_INNER:-0}" != 1 ]; then
    exec bash .oss-scanner/shell.sh env OSS_SCANNER_TEST_INNER=1 bash .oss-scanner/test.sh "$@"
fi
lwpc=$(bash qdwin/libweston-vendored/pkgconfig-dir.sh) || exit
export PKG_CONFIG_PATH="$lwpc:${PKG_CONFIG_PATH:-}"
# Reuse qci's pytest batching and per-file isolation, without Podman or VMs.
# shellcheck source=/dev/null
source ci/lib/gates/host.sh
rc=0
# Match qci's host-step wall-clock bound. A stuck Qt dialog must fail visibly
# while allowing the other independent suites to run.
step_timeout=${OSS_SCANNER_TEST_TIMEOUT:-600}
run() {
    echo "[scanner-test] ($PWD) $*"
    local status=0
    timeout --signal=TERM --kill-after=10 "$step_timeout" "$@" || status=$?
    if [ "$status" != 0 ]; then
        echo "[scanner-test] FAIL: exit $status (timeout limit ${step_timeout}s): $*" >&2
        rc=1
        return "$status"
    fi
}
pytest_group() { local dir=$1; shift; (cd "$dir" && run python3 -m pytest "$@") || rc=1; }
group=${1:-all}
case "$group" in all|smoke|root|native|apps|extensions) ;; *) echo "usage: $0 [all|smoke|root|native|apps|extensions]" >&2; exit 2;; esac
if [ "$group" = smoke ]; then
    pytest_group . -q tests/unit/test_broker_upload_lineage.py tests/unit/test_broker_subscriber_restart.py tests/unit/test_admin_widgets_logic.py
fi
if [[ "$group" = all || "$group" = root ]]; then
    # qci gives the much larger root suite its own 1800-second budget.
    step_timeout=${OSS_SCANNER_ROOT_TEST_TIMEOUT:-1800} \
        run bash -c "$(host_pytest_cmd 'find:tests/unit -name "test_*.py"' 30 '' '-q')"
    pytest_group . -q tests/unit/test_admin_widgets_logic.py tests/unit/test_broker_subscriber_restart.py tests/unit/test_broker_upload_lineage.py
fi
if [[ "$group" = all || "$group" = native || "$group" = smoke ]]; then
    for component in qdwin daemons qdshell; do
        run meson test -C "$component/build-oss" --print-errorlogs --num-processes "${OSS_SCANNER_JOBS:-2}"
    done
    if [ "$group" != smoke ]; then run bash -c 'cd qdshell && QDSHELL_BUILD_DIR=build-oss bash scripts/ci-local.sh --no-int'; fi
fi
if [[ "$group" = all || "$group" = apps ]]; then
    for entry in 'sdk/presentation:' 'qdgreeter:tests' 'qdlocker:tests/unit' 'qdfileman:' 'qnotebook:' 'qdterm:--ignore=tests/test_print_terminal.py'; do
        dir=${entry%%:*}; args=${entry#*:}
        step_budget=$step_timeout
        # The 1611-test terminal suite exceeds qci's 600s default on two CPUs
        # while still progressing. Keep a finite, separately tunable budget.
        if [ "$dir" = qdterm ]; then step_budget=${OSS_SCANNER_QDTERM_TEST_TIMEOUT:-1800}; fi
        (cd "$dir" && step_timeout=$step_budget run bash -c "$(host_pytest_cmd all 0 '' "$args -q")") || rc=1
    done
    (cd qdbrowser && run bash -c "$(host_pytest_cmd 'glob:tests/test_*.py' 1 '' '-q')") || rc=1
fi
if [[ "$group" = all || "$group" = extensions || "$group" = smoke ]]; then
    for component in qdchrome-extension qdfirefox-extension; do
        sibling=qdchrome-extension; [ "$component" != "$sibling" ] || sibling=qdfirefox-extension
        (cd "$component" && export QDISTRO_REQUIRE_SIBLING=1 QDISTRO_SIBLING_GOLDEN="$PWD/../$sibling/tests/fixtures/golden-frames.js" && run npm test && run npm run build) || rc=1
    done
fi
exit "$rc"
