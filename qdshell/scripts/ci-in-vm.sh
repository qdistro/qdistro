#!/usr/bin/env bash
# qdshell CI runner for in-VM execution.
#
# Local development: scripts/ci-local.sh runs qmltest on the host.
# But qmltestrunner on the host doesn't have the same Quickshell
# imports as the VM, and any test that exercises QML singletons
# importing qs.* needs a VM with the full qdshell deployed. This
# script runs the same test set INSIDE the visual VM, on the actual
# Qt6 install we ship against.
#
# Usage:
#   scripts/ci-in-vm.sh                      # default VM
#   QDISTRO_VM=other-vm scripts/ci-in-vm.sh  # different VM
#   scripts/ci-in-vm.sh --bats               # also run integration bats

set -euo pipefail

cd "$(dirname "$0")/.."

VM="${QDISTRO_VM:?set QDISTRO_VM to the libvirt domain name}"
QDISTRO_DIR="${QDISTRO_DIR:-../qdistro}"
VME="$QDISTRO_DIR/scripts/vm/vm-exec"
HTTP_PORT="${HTTP_PORT:-8765}"

if [ ! -x "$VME" ]; then
    echo "vm-exec not found at $VME" >&2
    exit 2
fi

RUN_BATS=0
for arg in "$@"; do
    case "$arg" in
        --bats) RUN_BATS=1 ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    esac
done

# 1. Bundle Tests/ + Helpers/ + qmldir hints into a tar.
TMPTAR="$(mktemp -t qdshell-tests.XXXXXX.tar)"
tar -cf "$TMPTAR" Tests/ Helpers/

# Stage on the http-server qdistro uses for in-VM file pickup; if it's
# not running, start it pointed at a scratch dir the VM bootstrap knows
# to fetch from.
STAGE_DIR="${QDSHELL_HTTP_STAGE:-/tmp/qdshell-stage}"
mkdir -p "$STAGE_DIR"
cp "$TMPTAR" "$STAGE_DIR/qdshell-tests.tar"

if ! ss -tln 2>/dev/null | grep -q ":$HTTP_PORT "; then
    (cd "$STAGE_DIR" && nohup python3 -m http.server "$HTTP_PORT" \
        >/tmp/qdshell-http.log 2>&1 &)
    sleep 1
fi

# 2. Push runner script.
RUNNER="$(mktemp -t in-vm-runner.XXXXXX.sh)"
cat > "$RUNNER" <<'EOF'
#!/bin/bash
set -e
mkdir -p /tmp/qdshell-tests
cd /tmp/qdshell-tests
curl -s -o tests.tar http://10.0.2.2:8765/qdshell-tests.tar
tar -xf tests.tar

if [ ! -x /usr/bin/qmltestrunner6 ]; then
    echo "qmltestrunner6 not installed in VM — install qt6-declarative-tools"
    exit 2
fi

PASS=0
FAIL=0
FILES=0
for t in Tests/tst_*.qml; do
    FILES=$((FILES + 1))
    out="$(QT_QPA_PLATFORM=offscreen qmltestrunner6 -input "$t" 2>&1 || true)"
    line="$(echo "$out" | grep -E '^Totals:' | tail -1)"
    p="$(echo "$line" | sed -nE 's/.*Totals: ([0-9]+) passed.*/\1/p')"
    f="$(echo "$line" | sed -nE 's/.* ([0-9]+) failed.*/\1/p')"
    p="${p:-0}"; f="${f:-0}"
    PASS=$((PASS + p))
    FAIL=$((FAIL + f))
    if [ "$f" -gt 0 ]; then
        printf 'FAIL  %s: %d passed, %d failed\n' "$(basename $t)" "$p" "$f"
        echo "$out" | grep -E '^FAIL' | sed 's/^/  /'
    else
        printf 'OK    %s: %d passed\n' "$(basename $t)" "$p"
    fi
done

echo
echo "===================="
echo "qmltest summary (in VM):"
echo "  files:   $FILES"
echo "  passed:  $PASS"
echo "  failed:  $FAIL"
echo "===================="

if [ "$FAIL" -gt 0 ]; then exit 1; fi
EOF
chmod +x "$RUNNER"
cp "$RUNNER" "$STAGE_DIR/qdshell-test-runner.sh"

# 3. Run inside VM.
echo "==> running qmltests inside $VM"
"$VME" "$VM" 'curl -s -o /tmp/r.sh http://10.0.2.2:8765/qdshell-test-runner.sh && chmod +x /tmp/r.sh && /tmp/r.sh'
RC=$?

# 4. Optional bats run.
if [ "$RUN_BATS" = 1 ]; then
    echo
    echo "==> running broker-e2e.bats inside qdistro repo"
    BATS_FILE="$QDISTRO_DIR/tests/integration/vm/broker-e2e.bats"
    if [ -f "$BATS_FILE" ]; then
        (cd "$QDISTRO_DIR" && QDISTRO_VM="$VM" bats "$BATS_FILE")
    else
        echo "  no $BATS_FILE — skipping"
    fi
fi

rm -f "$TMPTAR" "$RUNNER"
exit $RC
