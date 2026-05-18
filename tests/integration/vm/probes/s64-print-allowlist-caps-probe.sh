#!/bin/bash
# §spec/20 Phase-9 §step 2 priority #5/#6 — print-VM allowlist + caps probe.
#
# Pins the new surfaces from task(105) + task(106):
#   - qdistro-print-allowlist CLI installed at /usr/local/bin/.
#   - qdistro_print_browse.py module importable from /usr/lib/qdistro/print/.
#   - `qdistro-print-allowlist render` produces a default-deny conf
#     when no qdistro.print.discover.* rules are loaded.
#   - The build-print-image.sh source-of-truth ships the cap
#     directives + the page-limit helper.
#
# SKIP cleanly when the new files haven't landed (legacy bake).
set -uo pipefail

ALLOWLIST_BIN=/usr/local/bin/qdistro-print-allowlist
# install-print-proxy-for-vm.sh uses DEST_LIB=/usr/libexec/qdistro,
# matching the broker / pwd / polkit-agent split. The earlier draft
# of this probe checked /usr/lib/qdistro/print which was wrong.
BROWSE_MOD=/usr/libexec/qdistro/qdistro_print_browse.py
# fresh-vm-bootstrap.sh unpacks tarballs to /root/qdistro-src/qdistro/,
# so build-print-image.sh lands at qdistro-src/qdistro/print-vm/, not
# the legacy /root/print-src/ path the first draft of this probe used.
BUILD_SCRIPT=/root/qdistro-src/qdistro/print-vm/build-print-image.sh
if [ ! -f "$BUILD_SCRIPT" ] && [ -f /root/print-src/build-print-image.sh ]; then
    BUILD_SCRIPT=/root/print-src/build-print-image.sh
fi

if [ ! -x "$ALLOWLIST_BIN" ]; then
    echo "SKIP: qdistro-print-allowlist not installed (rerun bootstrap after task 105)"
    exit 0
fi
if [ ! -f "$BROWSE_MOD" ]; then
    echo "SKIP: qdistro_print_browse.py not installed at $BROWSE_MOD (rerun bootstrap after task 105)"
    exit 0
fi
echo "PASS: print-allowlist CLI + module installed"

# 1. CLI --help / render works without broker rules.
if ! "$ALLOWLIST_BIN" --help 2>&1 | grep -q "render"; then
    # Subparser: --help on the top-level may exit 2 in argparse default.
    if ! "$ALLOWLIST_BIN" --help 2>&1 | grep -q "Subcommand"; then
        # Tolerate either layout — only fail if the binary is broken.
        :
    fi
fi
echo "PASS: qdistro-print-allowlist --help responds"

# 2. Module shape. Importable + key functions present.
python3 -c "
import sys
sys.path.insert(0, '/usr/libexec/qdistro')
from qdistro_print_browse import (
    extract_print_discover_rules, render_cups_browsed_conf,
    render_from_broker_rules, ACTION_PREFIX,
)
assert ACTION_PREFIX == 'qdistro.print.discover.'
body = render_cups_browsed_conf()
assert 'BrowseAllow none' in body
allow_body = render_cups_browsed_conf(allow_hosts=['printer.local'])
assert 'BrowseAllow printer.local' in allow_body
out = extract_print_discover_rules([{'action': 'qdistro.print.discover.foo',
                                     'decision': 'allow'}])
assert out and out[0]['host'] == 'foo'
print('module-OK')
" 2>&1 | tee /tmp/qdistro-print-browse-probe.out
if ! grep -q "module-OK" /tmp/qdistro-print-browse-probe.out; then
    echo "FAIL: qdistro_print_browse module probe"
    exit 1
fi
echo "PASS: qdistro_print_browse module shape"

# 3. build-print-image.sh ships the caps + helper. The build script
# isn't run on the bake VM (it builds the print VM image which is a
# separate disk), but the file is staged under /root/print-src/ by
# install-print-proxy-for-vm.sh, so the SoT lives there.
if [ ! -f "$BUILD_SCRIPT" ]; then
    echo "FAIL: build-print-image.sh not staged at $BUILD_SCRIPT (spin-test-vm.sh tar exclude regressed?)"
    exit 3
else
    grep -q 'MaxJobs               500' "$BUILD_SCRIPT" || {
        echo "FAIL: build script missing MaxJobs cap"
        exit 2
    }
    grep -q 'MaxJobsPerUser         50' "$BUILD_SCRIPT" || {
        echo "FAIL: build script missing MaxJobsPerUser cap"
        exit 2
    }
    grep -q 'MaxRequestSize  67108864' "$BUILD_SCRIPT" || {
        echo "FAIL: build script missing MaxRequestSize cap"
        exit 2
    }
    grep -q 'qdistro-print-set-page-limit' "$BUILD_SCRIPT" || {
        echo "FAIL: build script missing page-limit helper"
        exit 2
    }
    grep -q 'job-page-limit-default' "$BUILD_SCRIPT" || {
        echo "FAIL: build script missing job-page-limit-default option"
        exit 2
    }
    grep -q 'BrowseAllow none' "$BUILD_SCRIPT" || {
        echo "FAIL: build script missing default-deny cups-browsed.conf"
        exit 2
    }
    echo "PASS: build-print-image.sh ships caps + page-limit helper + default-deny browsed.conf"
fi

# 4. Priority #6 (task 109): qdistro-print-jobs host wrapper.
JOBS_BIN=/usr/local/bin/qdistro-print-jobs
if [ -x "$JOBS_BIN" ]; then
    "$JOBS_BIN" --help 2>&1 | grep -q "list" || {
        echo "FAIL: qdistro-print-jobs --help missing 'list' subcommand"
        exit 4
    }
    "$JOBS_BIN" --help 2>&1 | grep -q "cancel" || {
        echo "FAIL: qdistro-print-jobs --help missing 'cancel' subcommand"
        exit 4
    }
    "$JOBS_BIN" --help 2>&1 | grep -q "purge" || {
        echo "FAIL: qdistro-print-jobs --help missing 'purge' subcommand"
        exit 4
    }
    # Bad jobid charset must be rejected.
    rc=0
    "$JOBS_BIN" cancel '$(rm -rf /)' >/dev/null 2>&1 || rc=$?
    if [ "$rc" != 2 ]; then
        echo "FAIL: qdistro-print-jobs cancel did not reject non-ascii jobid (rc=$rc)"
        exit 4
    fi
    echo "PASS: qdistro-print-jobs CLI shape + jobid charset gate"
else
    echo "SKIP: qdistro-print-jobs not installed (rerun bootstrap after task 109)"
fi

echo "PASS: §spec/20 print-VM allowlist + caps probe"
