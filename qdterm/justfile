# QTerminator development tasks
# Install: zypper/apt/dnf install just  (or `cargo install just`)
# List all recipes: just --list

# Default recipe: show available commands
default:
    @just --list

# Run QTerminator
run *ARGS:
    python3 -m qterminator {{ARGS}}

# Install all system dependencies (auto-detects distro)
install-deps:
    #!/usr/bin/env bash
    set -e
    if command -v zypper >/dev/null; then
        # openSUSE: pythonNNN- prefix matches the default python3
        PY=$(python3 -c "import sys; print(f'python{sys.version_info.major}{sys.version_info.minor}')")
        echo "Using openSUSE Python prefix: $PY"
        sudo zypper install -y \
            $PY-PyQt6 $PY-PyQt6-devel \
            qtermwidget-devel libqtermwidget6-2 qtermwidget-data \
            gcc-c++ cmake \
            $PY-pyqt-builder \
            $PY-pytest $PY-pytest-qt \
            $PY-setproctitle libnotify-tools tmux xdg-utils
    elif command -v apt-get >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y --no-install-recommends \
            python3-pyqt6 python3-pyqt6.qtsvg \
            libqtermwidget6-3 libqtermwidget6-3-dev qtermwidget-data \
            g++ cmake qmake6 \
            python3-pyqt-builder sip-tools \
            python3-pytest python3-pytest-qt \
            python3-setproctitle libnotify-bin tmux xdg-utils
    elif command -v dnf >/dev/null; then
        sudo dnf install -y \
            python3-pyqt6 python3-pyqt6-devel \
            qtermwidget-qt6-devel qtermwidget-qt6 \
            gcc-c++ cmake qt6-qtbase-devel \
            python3-pyqt-builder python3-sip-devel \
            python3-pytest python3-pytest-qt \
            python3-setproctitle libnotify tmux xdg-utils
    else
        echo "Unknown distribution. See README.md for manual install."
        exit 1
    fi

# Refresh vendored qtermwidget pyqt/ source from upstream tarball
# Usage: just update-vendor [tag-or-branch]  (default: 2.3.0)
update-vendor REF="2.3.0":
    util/update-vendor.sh {{REF}}

# Build and install QTermWidget Python SIP bindings from vendored source
build-sip:
    util/build-sip.sh

# Verify QTermWidget bindings are importable
verify:
    @python3 -c "from QTermWidget import QTermWidget; print('QTermWidget bindings: OK')"
    @python3 -c "from PyQt6.QtCore import QT_VERSION_STR; print(f'PyQt6 / Qt: {QT_VERSION_STR}')"
    @python3 --version

# Run the full test suite (each file in separate process to avoid PTY fd exhaustion)
test *ARGS:
    #!/usr/bin/env bash
    set -e
    echo "=== QTerminator tests ==="
    echo "Python: $(python3 --version)"
    echo ""
    for f in tests/test_*.py; do
        echo "--- $f ---"
        QT_QPA_PLATFORM=offscreen python3 -m pytest "$f" -v --tb=short {{ARGS}}
        echo ""
    done
    echo "=== All test files passed ==="

# Run tests in a single process (faster, but PTY fd risk)
test-fast:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v --tb=short

# Run a single test file
test-file FILE:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/{{FILE}} -v --tb=short

# Run tests matching a pattern
test-match PATTERN:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -v -k "{{PATTERN}}" --tb=short

# Run only the shortcut coverage check
test-shortcuts:
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_shortcut_coverage.py -v

# Compile all Python files (catches syntax errors)
compile:
    python3 -m compileall -q qterminator/ tests/

# Lint with ruff (if installed)
lint:
    @command -v ruff >/dev/null && ruff check qterminator/ tests/ || echo "Install ruff for linting"

# Clean __pycache__ and build artifacts
clean:
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name '*.pyc' -delete
    rm -rf build/ dist/ *.egg-info/ ci.log

# Render the SVG icon to PNG at all sizes
icon:
    QT_QPA_PLATFORM=offscreen python3 -c "\
    from PyQt6.QtSvg import QSvgRenderer; \
    from PyQt6.QtGui import QImage, QPainter; \
    from PyQt6.QtCore import Qt; \
    from PyQt6.QtWidgets import QApplication; \
    import sys; \
    app = QApplication(sys.argv); \
    r = QSvgRenderer('icons/qterminator.svg'); \
    [(\
        lambda s, i=QImage(s, s, QImage.Format.Format_ARGB32): (\
            i.fill(Qt.GlobalColor.transparent), \
            (lambda p: (r.render(p), p.end()))(QPainter(i)), \
            i.save(f'icons/qterminator-{s}.png'))) for s in [16,22,24,32,48,64,128,256,512]]"

# Install QTerminator system-wide (Linux)
install:
    pip install --user .
    install -Dm644 doc/qterminator.1 ~/.local/share/man/man1/qterminator.1
    install -Dm644 doc/qterminator-config.5 ~/.local/share/man/man5/qterminator-config.5
    install -Dm644 qterminator.desktop ~/.local/share/applications/qterminator.desktop
    install -Dm644 qterminator.metainfo.xml ~/.local/share/metainfo/qterminator.metainfo.xml
    install -Dm644 icons/qterminator.svg ~/.local/share/icons/hicolor/scalable/apps/qterminator.svg
    @for size in 16 22 24 32 48 64 128 256 512; do \
        install -Dm644 icons/qterminator-$size.png \
            ~/.local/share/icons/hicolor/${size}x${size}/apps/qterminator.png; \
    done
    @echo "Installed. Run: qterminator"

# Uninstall the user installation
uninstall:
    pip uninstall -y qterminator
    rm -f ~/.local/share/man/man1/qterminator.1
    rm -f ~/.local/share/man/man5/qterminator-config.5
    rm -f ~/.local/share/applications/qterminator.desktop
    rm -f ~/.local/share/metainfo/qterminator.metainfo.xml
    rm -f ~/.local/share/icons/hicolor/scalable/apps/qterminator.svg
    @for size in 16 22 24 32 48 64 128 256 512; do \
        rm -f ~/.local/share/icons/hicolor/${size}x${size}/apps/qterminator.png; \
    done

# Show test count by file
test-count:
    @QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ --co -q 2>&1 | grep -oE "^tests/[a-z_]+" | sort | uniq -c | sort -rn

# Render a man page preview
man:
    man -l doc/qterminator.1

# Extract translatable strings into po/qterminator.pot (requires xgettext)
pot:
    xgettext --language=Python \
        --keyword=_ --keyword=tr --keyword=ngettext:1,2 \
        --package-name=qterminator --package-version=0.1.0 \
        --copyright-holder="Jan Kotek" \
        --msgid-bugs-address=jan@kotek.net \
        --output=po/qterminator.pot \
        $(find qterminator/ -name '*.py')
    @echo "Extracted strings -> po/qterminator.pot"

# Compile all po/*.po into .mo files (requires msgfmt)
mo:
    #!/usr/bin/env bash
    set -e
    for po in po/*.po; do
        [ -f "$po" ] || continue
        lang=$(basename "$po" .po)
        mkdir -p "po/$lang/LC_MESSAGES"
        msgfmt -o "po/$lang/LC_MESSAGES/qterminator.mo" "$po"
        echo "  $po -> po/$lang/LC_MESSAGES/qterminator.mo"
    done

# Update an existing translation against the latest .pot
po-update LANG:
    msgmerge --update --backup=none po/{{LANG}}.po po/qterminator.pot

# Initialize a new translation
po-init LANG:
    msginit --locale={{LANG}} --input=po/qterminator.pot --output=po/{{LANG}}.po

# Build .deb and .rpm packages locally (requires fpm)
package VERSION="0.1.0":
    #!/usr/bin/env bash
    set -e
    if ! command -v fpm >/dev/null; then
        echo "Install fpm first: gem install --user-install fpm"
        exit 1
    fi
    rm -rf stage
    mkdir -p stage/usr/lib/python3/site-packages
    mkdir -p stage/usr/bin stage/usr/share/man/man1 stage/usr/share/man/man5
    mkdir -p stage/usr/share/applications stage/usr/share/metainfo
    cp -r qterminator stage/usr/lib/python3/site-packages/
    printf '#!/bin/bash\nexec python3 -m qterminator "$@"\n' > stage/usr/bin/qterminator
    chmod +x stage/usr/bin/qterminator
    cp doc/qterminator.1 stage/usr/share/man/man1/
    cp doc/qterminator-config.5 stage/usr/share/man/man5/
    cp qterminator.desktop stage/usr/share/applications/
    cp qterminator.metainfo.xml stage/usr/share/metainfo/
    for size in 16 22 24 32 48 64 128 256 512; do
        mkdir -p stage/usr/share/icons/hicolor/${size}x${size}/apps
        cp icons/qterminator-${size}.png stage/usr/share/icons/hicolor/${size}x${size}/apps/qterminator.png
    done
    mkdir -p stage/usr/share/icons/hicolor/scalable/apps
    cp icons/qterminator.svg stage/usr/share/icons/hicolor/scalable/apps/
    fpm -s dir -t deb -n qterminator -v {{VERSION}} --license GPL-3.0-only \
        --maintainer "jan@kotek.net" --architecture all -C stage usr
    fpm -s dir -t rpm -n qterminator -v {{VERSION}} --license GPL-3.0-only \
        --maintainer "jan@kotek.net" --architecture noarch -C stage usr
    rm -rf stage
    ls -lh *.deb *.rpm
