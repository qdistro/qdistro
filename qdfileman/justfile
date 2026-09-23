# QFileMan development tasks

# Default recipe: show available commands
default:
    @just --list

# Run QFileMan
run *ARGS:
    python3 -m qfileman {{ARGS}}

# Run tests
test *ARGS:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v --tb=short {{ARGS}}

# Run a single test file
test-file FILE:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/{{FILE}} -v --tb=short

# Run tests matching a pattern
test-match PATTERN:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v -k "{{PATTERN}}" --tb=short

# Compile all Python files (catches syntax errors)
compile:
    python3 -m compileall -q qfileman/ tests/

# Lint with ruff (if installed)
lint:
    ruff check qfileman/ tests/ || true

# Install the package in development mode
install:
    pip install -e .

# Format code with ruff
format:
    ruff format qfileman/ tests/ || true
