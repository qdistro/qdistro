#!/bin/bash
# fresh-vm-bootstrap.sh — run inside a freshly-cloned baseweed VM to:
#   1. Fetch the three qdistro repos as tarballs from host:8765.
#   2. Build qdwin from source (libweston shell plugin).
#   3. Build qdistro's C daemons against qdwin's protocol XML.
#   4. Install the Python broker / polkit-agent / pwd / etc. services.
#   5. Install the qdshell QML stack.
#   6. Wire greetd to start qdwin + qdshell on tty3.
#
# Prerequisites (handled by build-baked-baseweed.sh):
#   - SELinux permissive
#   - admin user (uid 1000) present
#   - meson + ninja + libweston-14-devel + wayland-protocols
#   - quickshell + qt6-* for qdshell
#   - bats for in-VM integration tests
#
# Host must be serving the three tarballs at http://10.0.2.2:8765/:
#   /qdistro.tar.gz
#   /qdwin.tar.gz
#   /qdshell.tar.gz
#
# spin-test-vm.sh handles the host-side staging. To bootstrap manually:
#   STAGE=$(mktemp -d)
#   tar czf $STAGE/qdistro.tar.gz -C ~/path/to/qdistro .
#   tar czf $STAGE/qdwin.tar.gz   -C ~/path/to/qdwin .
#   tar czf $STAGE/qdshell.tar.gz -C ~/path/to/qdshell .
#   cp ~/path/to/qdistro/scripts/vm/fresh-vm-bootstrap.sh $STAGE/
#   (cd $STAGE && python3 -m http.server 8765 --bind 127.0.0.1) &
#   vm-exec <vm> "wget -O- http://10.0.2.2:8765/fresh-vm-bootstrap.sh | bash"

set -eo pipefail

HOST="${QDISTRO_HTTP_HOST:-http://10.0.2.2:8765}"
SRC=/root/qdistro-src

log() { echo "[bootstrap] $*"; }

# ---- 1. Fetch + unpack the three repos -----------------------------------
log "fetching tarballs from $HOST..."
mkdir -p "$SRC"/{qdistro,qdwin,qdshell}
for repo in qdistro qdwin qdshell; do
    wget -q -O "/tmp/$repo.tar.gz" "$HOST/$repo.tar.gz" \
        || { echo "[bootstrap] failed to fetch $HOST/$repo.tar.gz"; exit 2; }
    tar -xzf "/tmp/$repo.tar.gz" -C "$SRC/$repo"
    rm -f "/tmp/$repo.tar.gz"
done

# ---- 2. Build qdwin ------------------------------------------------------
log "building qdwin (libweston shell plugin)..."
cd "$SRC/qdwin"
meson setup build --wipe
meson compile -C build
meson install -C build

# ---- 3. Build qdistro daemons (C, against ../qdwin XML) ------------------
log "building qdistro daemons..."
cd "$SRC/qdistro/daemons"
meson setup build --wipe
meson compile -C build
meson install -C build

# ---- 4. Install Python modules + systemd units --------------------------
log "installing Python modules..."
cd "$SRC/qdistro"
# Each module's install-*.sh handles its own per-module install. Order
# matters only for the broker (others depend on it).
for installer in \
    scripts/install/install-broker-for-qdwin.sh \
    scripts/install/install-polkit-agent-for-vm.sh \
    scripts/install/install-pwd-for-vm.sh \
    scripts/install/install-qsu-for-vm.sh \
    scripts/install/install-browser-bridge-for-vm.sh \
    scripts/install/install-phone-for-vm.sh \
    scripts/install/install-print-proxy-for-vm.sh \
    scripts/install/install-recall-for-vm.sh \
    scripts/install/install-snapshots-for-vm.sh; do
    if [ -x "$installer" ]; then
        log "  running $(basename "$installer")..."
        bash "$installer" || { echo "[bootstrap] $installer failed"; exit 3; }
    fi
done

# ---- 5. Install SELinux policy modules (permissive by default) ----------
log "installing SELinux policy modules (permissive)..."
for pol in selinux/broker selinux/pwd selinux/tier1; do
    if [ -d "$pol" ] && [ -x "$pol/install-policy.sh" ]; then
        (cd "$pol" && bash install-policy.sh) || log "  WARN: $pol install failed"
    fi
done

# ---- 6. Install qdshell QML stack ---------------------------------------
log "installing qdshell QML..."
install -d -o admin -g users /home/admin/.config/quickshell/qdshell
cp -r "$SRC/qdshell"/* /home/admin/.config/quickshell/qdshell/
chown -R admin:users /home/admin/.config/quickshell/qdshell

# ---- 7. Greetd wiring ---------------------------------------------------
if [ -f "$SRC/qdistro/deploy/greetd-config.toml" ]; then
    log "wiring greetd..."
    install -m 0644 "$SRC/qdistro/deploy/greetd-config.toml" /etc/greetd/config.toml
fi

log "bootstrap complete."
log "to start the qdwin session: systemctl start greetd.service"
