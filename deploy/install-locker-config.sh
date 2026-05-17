#!/bin/bash
# Install locker configuration file

set -e

CONFIG_SRC="./etc/qdistro/locker.conf"
SYSTEM_CONFIG_DIR="/etc/qdistro"

# Create the config directory if it doesn't exist
sudo mkdir -p "$SYSTEM_CONFIG_DIR"

# Copy the config file if it doesn't already exist or if we're forcing an update
if [ ! -f "$SYSTEM_CONFIG_DIR/locker.conf" ]; then
    sudo cp "$CONFIG_SRC" "$SYSTEM_CONFIG_DIR/"
    echo "Installed locker configuration to $SYSTEM_CONFIG_DIR/locker.conf"
else
    echo "Configuration file already exists at $SYSTEM_CONFIG_DIR/locker.conf"
    echo "Skipping installation to preserve user settings"
fi

# Set appropriate permissions
sudo chmod 644 "$SYSTEM_CONFIG_DIR/locker.conf"

echo "Locker configuration installation complete"