#!/bin/bash
# Compatibility wrapper for the current tier-4 guest image builder.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/../tier4-vm-guest/build-guest-image.sh" "$@"
