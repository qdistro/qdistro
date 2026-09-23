#!/bin/bash
# Refresh vendored qtermwidget pyqt/ source from upstream tarball.
#
# Usage:
#   util/update-vendor.sh              # default: 2.3.0 tag
#   util/update-vendor.sh 2.4.0        # specific tag
#   util/update-vendor.sh master       # branch tarball
#
# Downloads a release tarball (no git clone) and replaces
# qtermwidget-pyqt/{project.py,pyproject.toml,sip} in place.

set -euo pipefail

REF="${1:-2.3.0}"

# If REF doesn't look like a tag (no digits), treat as a branch
if [[ ! "$REF" =~ [0-9] ]]; then
    URL="https://github.com/lxqt/qtermwidget/archive/refs/heads/$REF.tar.gz"
else
    URL="https://github.com/lxqt/qtermwidget/archive/refs/tags/$REF.tar.gz"
fi

echo "Downloading: $URL"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR_DIR="$SCRIPT_DIR/../qtermwidget-pyqt"

SRC=$(mktemp -d)
trap "rm -rf '$SRC'" EXIT

curl -fsSL "$URL" | tar xz -C "$SRC" --strip-components=1

if [ ! -d "$SRC/pyqt" ]; then
    echo "Error: upstream has no pyqt/ directory at this ref"
    exit 1
fi

rm -rf "$VENDOR_DIR/project.py" "$VENDOR_DIR/pyproject.toml" "$VENDOR_DIR/sip"
cp -r "$SRC/pyqt/project.py" "$SRC/pyqt/pyproject.toml" "$SRC/pyqt/sip" "$VENDOR_DIR/"

# Update the version note in README
sed -i "s|version [0-9a-f.]\\+).|version $REF).|" "$VENDOR_DIR/README.md" 2>/dev/null || true

echo
echo "Vendored qtermwidget $REF. Diff:"
git -C "$(dirname "$VENDOR_DIR")" diff --stat qtermwidget-pyqt/ 2>/dev/null || true
