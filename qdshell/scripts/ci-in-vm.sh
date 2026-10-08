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
QDISTRO_DIR="${QDISTRO_DIR:-..}"   # monorepo root (qdshell/ is in-tree)
VME="$QDISTRO_DIR/scripts/vm/vm-exec"
# HTTP_PORT may request an explicit staging port; default is 0 = kernel
# assigns a free one (fixed host ports collide across test users).

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

# Stage into a private, unpredictable dir served by OUR OWN http.server bound
# to a kernel-assigned port. Do NOT reuse a fixed port or an already-running
# listener: host ports are shared by every test user on this machine, so a
# stale foreign server on :8765 would hand the guest another user's stage
# dir (404s or wrong content). Binding port 0 removes the probe->bind race.
# A predictable /tmp stage dir is equally unsafe — a foreign user could
# pre-create it and swap content after we copy, so the default is mktemp.
if [ -n "${QDSHELL_HTTP_STAGE:-}" ]; then
    STAGE_DIR="$QDSHELL_HTTP_STAGE"
    mkdir -p "$STAGE_DIR"
    CLEAN_STAGE=0
else
    STAGE_DIR="$(mktemp -d -t qdshell-stage.XXXXXX)"
    CLEAN_STAGE=1
fi
cp "$TMPTAR" "$STAGE_DIR/qdshell-tests.tar"

HTTP_LOG="$(mktemp -t qdshell-http.XXXXXX.log)"
PORT_FILE="$(mktemp -t qdshell-http-port.XXXXXX)"
: > "$PORT_FILE"
# An explicit HTTP_PORT env still works; the server prints the port it
# ACTUALLY bound, which doubles as proof we (not a foreign listener) own it.
REQ_PORT="${HTTP_PORT:-0}"
(
    cd "$STAGE_DIR" || exit 1
    exec python3 -c '
import http.server, socketserver, sys
socketserver.TCPServer.allow_reuse_address = (sys.argv[2] == "1")
httpd = socketserver.TCPServer(("0.0.0.0", int(sys.argv[1])), http.server.SimpleHTTPRequestHandler)
sys.stdout.write(str(httpd.server_address[1]) + "\n"); sys.stdout.flush()
httpd.serve_forever()
' "$REQ_PORT" "$([ "$REQ_PORT" = 0 ] && echo 0 || echo 1)" >"$PORT_FILE" 2>"$HTTP_LOG"
) &
HTTP_PID=$!
trap 'kill "$HTTP_PID" 2>/dev/null || true; [ "$CLEAN_STAGE" = 1 ] && rm -rf "$STAGE_DIR"' EXIT
for _ in $(seq 1 50); do
    HTTP_PORT=$(head -1 "$PORT_FILE" 2>/dev/null | tr -dc '0-9')
    [ -n "$HTTP_PORT" ] && break
    kill -0 "$HTTP_PID" 2>/dev/null || break
    sleep 0.2
done
if [ -z "$HTTP_PORT" ]; then
    echo "staging HTTP server failed to bind (see $HTTP_LOG)" >&2
    tail -5 "$HTTP_LOG" >&2 || true
    exit 2
fi
STAGE_URL="http://10.0.2.2:$HTTP_PORT"
echo "==> staging server: $STAGE_URL (dir $STAGE_DIR, pid $HTTP_PID)"

# 2. Push runner script.
RUNNER="$(mktemp -t in-vm-runner.XXXXXX.sh)"
cat > "$RUNNER" <<'EOF'
#!/bin/bash
set -e
mkdir -p /tmp/qdshell-tests
cd /tmp/qdshell-tests
curl -sf -o tests.tar "$QDSHELL_STAGE_URL/qdshell-tests.tar"
tar -xf tests.tar

QMLTEST_BIN=${QMLTESTRUNNER:-/usr/bin/qmltestrunner6}
if [ ! -x "$QMLTEST_BIN" ]; then
    echo "qmltestrunner not executable: $QMLTEST_BIN — install qt6-declarative-tools" >&2
    exit 2
fi

# A missing match used to leave the literal "Tests/tst_*.qml", produce no
# Totals line, and count as 0 failed. A crash after a clean Totals line
# passed because `|| true` discarded the runner status.
shopt -s nullglob
qml_files=(Tests/tst_*.qml)
shopt -u nullglob
if [ "${#qml_files[@]}" -eq 0 ]; then
    echo "no Tests/tst_*.qml — refusing an empty qmltest suite" >&2
    exit 1
fi

PASS=0
FAIL=0
FILES=0
for t in "${qml_files[@]}"; do
    FILES=$((FILES + 1))
    rc=0
    out="$(QT_QPA_PLATFORM=offscreen "$QMLTEST_BIN" -input "$t" 2>&1)" || rc=$?
    line="$(printf '%s\n' "$out" | grep -E '^Totals:' | tail -1 || true)"
    if [ -z "$line" ]; then
        printf 'FAIL  %s: no Totals line (exit %d)\n' "$(basename "$t")" "$rc" >&2
        FAIL=$((FAIL + 1))
        continue
    fi
    p="$(echo "$line" | sed -nE 's/.*Totals: ([0-9]+) passed.*/\1/p')"
    f="$(echo "$line" | sed -nE 's/.* ([0-9]+) failed.*/\1/p')"
    p="${p:-0}"; f="${f:-0}"
    PASS=$((PASS + p))
    FAIL=$((FAIL + f))
    if [ "$f" -gt 0 ]; then
        printf 'FAIL  %s: %d passed, %d failed (exit %d)\n' "$(basename "$t")" "$p" "$f" "$rc"
        echo "$out" | grep -E '^FAIL' | sed 's/^/  /' || true
    elif [ "$rc" -ne 0 ]; then
        printf 'FAIL  %s: qmltestrunner exited %d after reporting %d passed, 0 failed\n' \
            "$(basename "$t")" "$rc" "$p" >&2
        FAIL=$((FAIL + 1))
    else
        printf 'OK    %s: %d passed\n' "$(basename "$t")" "$p"
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
"$VME" "$VM" "curl -sf -o /tmp/r.sh '$STAGE_URL/qdshell-test-runner.sh' && chmod +x /tmp/r.sh && QDSHELL_STAGE_URL='$STAGE_URL' /tmp/r.sh"
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
