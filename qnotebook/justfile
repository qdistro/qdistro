# qnotebook development tasks
# Install: zypper/apt/dnf install just
# List recipes: just --list

export PYTHONPATH := env_var_or_default("PYTHONPATH", "")

# Default: show available commands
default:
    @just --list

# Run qnotebook
run *ARGS:
    python3 -m qnotebook {{ARGS}}

# Install all system dependencies (auto-detects distro)
install-deps:
    #!/usr/bin/env bash
    set -e
    if command -v zypper >/dev/null; then
        PY=$(python3 -c "import sys; print(f'python{sys.version_info.major}{sys.version_info.minor}')")
        echo "Using openSUSE Python prefix: $PY"
        sudo zypper install -y \
            $PY-PyQt6 $PY-PyQt6-devel \
            $PY-pytest $PY-pytest-qt \
            $PY-mistune $PY-Pillow
    elif command -v apt-get >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends \
            python3-pyqt6 python3-pyqt6.qtsvg \
            python3-pytest python3-pytest-qt \
            python3-mistune python3-pil
    elif command -v dnf >/dev/null; then
        sudo dnf install -y \
            python3-pyqt6 python3-pyqt6-devel \
            python3-pytest python3-pytest-qt \
            python3-mistune python3-pillow
    else
        echo "Unknown distribution."
        exit 1
    fi

# Sanity-check environment: PyQt6 + mistune present
verify:
    @python3 -c "from PyQt6.QtCore import QT_VERSION_STR; print(f'PyQt6 / Qt: {QT_VERSION_STR}')"
    @python3 -c "import mistune; print(f'mistune: {mistune.__version__}')"
    @python3 --version

# Run the full test suite (offscreen Qt)
test *ARGS:
    #!/usr/bin/env bash
    set -e
    echo "=== qnotebook tests ==="
    echo "Python: $(python3 --version)"
    echo ""
    for f in tests/test_*.py; do
        [ -f "$f" ] || continue
        echo "--- $f ---"
        QT_QPA_PLATFORM=offscreen python3 -m pytest "$f" -v --tb=short {{ARGS}}
        echo ""
    done
    echo "=== All test files passed ==="

# Run tests in a single process (faster)
test-fast:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v --tb=short

# Run a single test file
test-file FILE:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/{{FILE}} -v --tb=short

# Run tests matching a pattern
test-match PATTERN:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v -k "{{PATTERN}}" --tb=short

# Report which SafeWriter merge-ladder rungs are live
check-merge-tools:
    @python3 -c "from qnotebook import safe_save as s; print(f'git merge-file: {s.HAS_GIT_MERGE_FILE}'); print(f'wiggle:         {s.HAS_WIGGLE}'); print(f'mergiraf:       {s.HAS_MERGIRAF}')"

# Compile all Python (catches syntax errors)
compile:
    python3 -m compileall -q qnotebook/ tests/

# Lint with ruff (if installed)
lint:
    @command -v ruff >/dev/null && ruff check qnotebook/ tests/ || echo "Install ruff for linting"

# Clean __pycache__ and build artifacts
clean:
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name '*.pyc' -delete
    rm -rf build/ dist/ *.egg-info/ ci.log

# Show test count per file
test-count:
    @QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ --co -q 2>&1 | grep -oE "^tests/[a-z_]+" | sort | uniq -c | sort -rn
