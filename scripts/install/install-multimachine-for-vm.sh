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
              mm_pairing_authority.py mm_remote_session_authority.py \
              mm_display_authority.py \
              mm_display_carrier_launcher.py \
              mm_remote_session_launcher.py mm_session_launcher.py \
              origin_authority.py \
              rdp_client_wrapper.py remote_adapter.py \
              remote_nested_protocol.py remote_nested_service.py \
              remote_nested_supervisor.py remote_nested_registry.py \
              remote_display_slot.py display_slot_controller.py \
              display_dock_session.py display_dock_service.py \
              display_dock_rpc.py mm_display_dock_daemon.py \
              display_shell_mailbox.py \
              display_shell_service.py \
              display_carrier.py display_carrier_endpoint.py \
              display_panel_agent.py display_panel_endpoint.py \
              mm_display_panel_launcher.py \
              remote_adapter_transport.py \
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
install -o root -g root -m 0755 "$SRC/qdistro-mm-remote-session-launcher" \
    /usr/local/bin/qdistro-mm-remote-session-launcher
install -o root -g root -m 0755 "$SRC/qdistro-mm-remote-adapter" \
    /usr/local/bin/qdistro-mm-remote-adapter
install -o root -g root -m 0755 "$SRC/qdistro-mm-display-carrier-launcher" \
    /usr/local/bin/qdistro-mm-display-carrier-launcher
install -o root -g root -m 0755 "$SRC/qdistro-mm-display-carrier" \
    /usr/local/bin/qdistro-mm-display-carrier
install -o root -g root -m 0755 "$SRC/qdistro-mm-display-panel-launcher" \
    /usr/local/bin/qdistro-mm-display-panel-launcher
install -o root -g root -m 0755 "$SRC/qdistro-mm-display-panel" \
    /usr/local/bin/qdistro-mm-display-panel
install -o root -g root -m 0755 "$SRC/qdistro-mm-display-dock" \
    /usr/local/bin/qdistro-mm-display-dock
install -o root -g root -m 0644 "$SRC/qdistro-mm-display-dock.service" \
    /etc/systemd/system/qdistro-mm-display-dock.service
install -o root -g root -m 0755 "$SRC/qdistro-mm-remote-nested-controller" \
    /usr/local/bin/qdistro-mm-remote-nested-controller
install -o root -g root -m 0755 "$SRC/qdistro-mm-remote-nested-session" \
    /usr/local/bin/qdistro-mm-remote-nested-session
install -o root -g root -m 0755 "$SRC/qdistro-mm-rdp-client-wrapper" \
    /usr/local/bin/qdistro-mm-rdp-client-wrapper

systemctl daemon-reload
echo "qdistro multi-machine runtime installed (display dock remains inert until configured)"
