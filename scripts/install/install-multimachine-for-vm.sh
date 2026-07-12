#!/bin/bash
# Install the inert-by-default multi-machine display runtime.  The broker is
# started explicitly by the pairing/session orchestrator with its secret-bearing
# JSON session description on an inherited fd; it is deliberately not enabled as
# an always-on user unit.
set -eu

SRC=${1:-/root/qdistro-src/qdistro/multimachine}
DEST=/usr/local/lib/qdistro/multimachine

if [ ! -d "$SRC" ]; then
    echo "ERROR: multimachine source not found at $SRC" >&2
    exit 2
fi

install -d -o root -g root -m 0755 "$DEST/harness" /usr/local/bin
for module in __init__.py bridge.py control_source.py mm_broker.py \
              mm_pairing_authority.py mm_session_launcher.py origin_authority.py \
              rdp_client_wrapper.py \
              sidechannel.py; do
    install -o root -g root -m 0644 "$SRC/$module" "$DEST/$module"
done
install -o root -g root -m 0644 "$SRC/harness/__init__.py" \
    "$DEST/harness/__init__.py"
install -o root -g root -m 0644 "$SRC/harness/viewer_broker.py" \
    "$DEST/harness/viewer_broker.py"
install -o root -g root -m 0755 "$SRC/qdistro-mm-broker" \
    /usr/local/bin/qdistro-mm-broker
install -o root -g root -m 0755 "$SRC/qdistro-mm-session-launcher" \
    /usr/local/bin/qdistro-mm-session-launcher
install -o root -g root -m 0755 "$SRC/qdistro-mm-rdp-client-wrapper" \
    /usr/local/bin/qdistro-mm-rdp-client-wrapper

echo "qdistro multi-machine runtime installed (not auto-started)"
