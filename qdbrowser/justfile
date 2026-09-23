# qdbrowser task runner. Mirrors qterminator's justfile conventions.

set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

default:
    @just --list

# Install system deps. Detects openSUSE / Ubuntu / Fedora.
install-deps:
    #!/usr/bin/env bash
    set -eu
    if command -v zypper >/dev/null; then
        sudo zypper install -y python313-PyQt6 python313-PyQt6-WebEngine \
            python313-pytest python313-pytest-qt python313-Pillow just
    elif command -v apt >/dev/null; then
        sudo apt install -y python3-pyqt6 python3-pyqt6.qtwebenginewidgets \
            python3-pytest python3-pytest-qt python3-pil just
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3-pyqt6 python3-pyqt6-webengine \
            python3-pytest python3-pytest-qt python3-pillow just
    else
        echo "Unknown distro; install PyQt6 + PyQt6-WebEngine + pytest + pytest-qt + pillow manually." >&2
        exit 1
    fi

# Install MCP support for agent-driving (optional).
install-mcp:
    pip install --user mcp

# Install qdbrowser in editable mode.
install:
    pip install --user -e .

# Run qdbrowser.
run *ARGS:
    python3 -m qdbrowser {{ARGS}}

# Run qdbrowser with agent_control enabled (Unix socket exposed).
run-agent *ARGS:
    QDBROWSER_AGENT_CONTROL=1 python3 -m qdbrowser {{ARGS}}

# Headless tests. Each test file in its own subprocess (avoids
# QWebEngineProfile / fd leaks across tests).
test:
    #!/usr/bin/env bash
    set -eu
    export QT_QPA_PLATFORM=offscreen
    export QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --headless"
    fail=0
    for f in tests/test_*.py; do
        echo "=== $f ==="
        python3 -m pytest -x "$f" || fail=1
    done
    exit $fail

# Run a single test file or pattern.
test-file FILE:
    QT_QPA_PLATFORM=offscreen \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --headless" \
    python3 -m pytest -x -v {{FILE}}

test-match PATTERN:
    QT_QPA_PLATFORM=offscreen \
    QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu --headless" \
    python3 -m pytest -x -k '{{PATTERN}}'

# VM integration tests (libvirt + bats). Mirrors qdistro's pattern.
test-vm:
    tests/integration/vm/run-parallel.sh

# Quick smoke: launch, navigate, screenshot, exit.
smoke:
    python3 scripts/smoke.py
