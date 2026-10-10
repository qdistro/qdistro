#!/usr/bin/env bash
# Local review only; no submission, mounts, privileged mode or network access.
set -euo pipefail
cd "$(dirname "$0")/.."
engine=${CONTAINER_ENGINE:-podman}
image=${1:-localhost/qdistro/oss-scanner:local}
test_group=${2:-smoke}
case "$test_group" in smoke|all|root|native|apps|extensions) ;;
    *) echo "usage: $0 [IMAGE [smoke|all|root|native|apps|extensions]]" >&2; exit 2;;
esac
logs=${OSS_SCANNER_LOG_DIR:-ci/runs/oss-scanner-local}
mkdir -p "$logs"
# Resolve once: a concurrent rebuild of the tag cannot change this check.
image_id=$("$engine" image inspect --format '{{.Id}}' "$image")
"$engine" image inspect "$image_id" > "$logs/image.json"
printf '%s\n' "$image_id" > "$logs/image-id.txt"
# Default runtime constraints match Anthropic's audit machine. If the host
# cannot delegate CPU/memory controllers, explicitly set resource limits to 0
# and record that the resource envelope was not verified.
limits=()
if [ "${OSS_SCANNER_RESOURCE_LIMITS:-1}" = 1 ]; then
    limits=(--cpus=2 --memory=8g)
fi
runtime_security=(--security-opt=label=disable --cap-add=SYS_PTRACE)
# Docker's default seccomp profile blocks the user namespaces needed by
# the real, empty Podman image-store queries in GC tests. The scanner VM
# uses a privileged container; this local hook only relaxes the syscall filter.
if [ "${engine##*/}" = docker ]; then
    runtime_security+=(--security-opt=seccomp=unconfined)
fi
# Match qci's headless fixture environment on SELinux hosts. Container labels
# otherwise identify stub trusted peers as hostile container_t processes.
# Actual enforcing-SELinux validation belongs in disposable qdistro VMs.
# Expressions in the script are expanded inside the offline container.
status=0
# shellcheck disable=SC2016
"$engine" run --rm --init --network=none "${runtime_security[@]}" "${limits[@]}" --env OSS_SCANNER_TEST_GROUP="$test_group" --workdir=/src "$image_id" \
    bash -euo pipefail -c '
        python3 - <<"PYNET"
import socket
from pathlib import Path
routes = Path("/proc/net/route").read_text().splitlines()[1:]
assert not any(row.split()[1] == "00000000" for row in routes), routes
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("1.1.1.1", 443))
except OSError as e:
    print("OFFLINE: no default route; external TCP blocked:", e)
else:
    raise SystemExit("FAIL: external TCP is reachable")
finally:
    s.close()
PYNET
        for component in qdchrome-extension qdfirefox-extension; do
            (cd "$component" && npm ci --offline --cache /tmp/qci-npm --no-audit --no-fund)
        done
        # Force a compile in both vendored dependencies and each native component.
        touch qdwin/libweston-vendored/src/libweston/compositor.c \
            qdshell/quickshell-vendored/src/src/core/types.cpp \
            qdwin/qdwin/qdwin.c daemons/secctx-exec/qdistro-secctx-exec.c \
            qdshell/qml-plugin/broker-call.cpp qsu/qsu.c
        # Recreate the small Meson builds to exercise offline configuration.
        rm -rf qdwin/build-oss daemons/build-oss qdshell/build-oss
        python3 -m pip uninstall -y QTermWidget
        bash .oss-scanner/build.sh
        bash .oss-scanner/build-sanitized.sh qdwin
        bash .oss-scanner/build-sanitized.sh daemons
        chown -R scanner:scanner /src
        # Preserve failures, but still collect the independent sanitizer results.
        test_status=0
        bash .oss-scanner/test.sh "$OSS_SCANNER_TEST_GROUP" || test_status=1
        bash .oss-scanner/shell.sh meson test -C qdwin/build-oss-sanitized \
            --suite logic --print-errorlogs --num-processes 2 || test_status=1
        bash .oss-scanner/shell.sh meson test -C daemons/build-oss-sanitized \
            --print-errorlogs --num-processes 2 || test_status=1
        exit "$test_status"
    ' > "$logs/offline.log" 2>&1 || status=$?
printf '%s\n' "$status" > "$logs/exit-code.txt"
if [ "$status" != 0 ]; then
    printf 'Offline check failed (exit %s). Log: %s/offline.log\n' "$status" "$logs" >&2
    exit "$status"
fi
printf 'Offline rebuild and %s tests passed. Log: %s/offline.log\n' "$test_group" "$logs"
