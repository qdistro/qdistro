#!/bin/bash
# Build and install QTermWidget Python SIP bindings from vendored source.
#
# Usage:
#   util/build-sip.sh                 # build from ./qtermwidget-pyqt
#   SIP_SRC=/path util/build-sip.sh   # build from a specific dir
#
# Requires the system to have:
#   - python3 with PyQt6 + sip-tools + pyqt-builder
#   - qmake6
#   - qtermwidget development headers (qtermwidget-devel)
#   - g++/gcc-c++

set -euo pipefail

if python3 -c "from QTermWidget import QTermWidget" 2>/dev/null; then
    echo "QTermWidget Python bindings already installed."
    exit 0
fi

QMAKE="${QMAKE:-$(command -v qmake6 || command -v qmake-qt6 || true)}"
if [ -z "$QMAKE" ]; then
    echo "Error: qmake6 not found. Install qt6-qtbase-devel (Fedora),"
    echo "qmake6 (Ubuntu), or libqt6-qtbase-devel (openSUSE)."
    exit 1
fi
echo "Using qmake: $QMAKE"

PYQT_BINDINGS=$(python3 -c "import PyQt6, os; print(os.path.join(os.path.dirname(PyQt6.__file__), 'bindings'))")
echo "PyQt6 SIP bindings: $PYQT_BINDINGS"

QT_INCLUDE=""
for d in /usr/include/qtermwidget6 /usr/include/qtermwidget-qt6 /usr/local/include/qtermwidget6; do
    if [ -d "$d" ]; then
        QT_INCLUDE="$d"
        break
    fi
done
if [ -z "$QT_INCLUDE" ]; then
    echo "Error: qtermwidget development headers not found."
    echo "Install qtermwidget-devel (openSUSE/Fedora) or libqtermwidget6-3-dev (Ubuntu)."
    exit 1
fi
echo "QTermWidget headers: $QT_INCLUDE"

# Locate vendored SIP source
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIP_SRC="${SIP_SRC:-$SCRIPT_DIR/../qtermwidget-pyqt}"
if [ ! -d "$SIP_SRC" ]; then
    echo "Error: vendored SIP source not found at $SIP_SRC"
    exit 1
fi

BUILD_DIR=$(mktemp -d)
cp -r "$SIP_SRC"/* "$BUILD_DIR/"
cd "$BUILD_DIR"

if ! grep -q "sip-include-dirs" pyproject.toml; then
    printf '\n[tool.sip.project]\nsip-include-dirs = ["%s"]\n' "$PYQT_BINDINGS" >> pyproject.toml
fi
if ! grep -q "$QT_INCLUDE" project.py; then
    sed -i "s|self.libraries.append('qtermwidget6')|self.libraries.append('qtermwidget6')\n        self.include_dirs.append('$QT_INCLUDE')|" project.py
fi

sip-wheel --qmake "$QMAKE"

# sip-wheel emits {project-name}-*.whl. The pyproject "name" has varied between
# vendor versions (QTermWidget / qtermwidget), so match case-insensitively.
shopt -s nullglob nocaseglob
wheels=( qtermwidget-*.whl )
shopt -u nullglob nocaseglob
if [ ${#wheels[@]} -eq 0 ]; then
    echo "Error: no QTermWidget wheel produced by sip-wheel"
    exit 1
fi
WHL="${wheels[0]}"
echo "Built: $WHL"
# --no-deps avoids pulling a newer PyPI PyQt6 over the distro one, which would
# need a Qt private API absent from system libQt6Core.
#
# Install destination: venv if active, else --user, else --break-system-packages
# for PEP 668 distro Pythons.
if [ -n "${VIRTUAL_ENV:-}" ]; then
    pip install --no-deps --force-reinstall "$WHL"
else
    pip install --no-deps --user --force-reinstall "$WHL" 2>&1 || \
        pip install --no-deps --break-system-packages --force-reinstall "$WHL"
fi

rm -rf "$BUILD_DIR"
echo "Done. Verify with: python3 -c 'from QTermWidget import QTermWidget'"
