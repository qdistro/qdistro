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
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

# ---- 3. Build qdistro daemons (C, against ../qdwin XML) ------------------
log "building qdistro daemons..."
cd "$SRC/qdistro/daemons"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

# ---- 4. Install Python modules + systemd units --------------------------
# Each install-*.sh takes the module's source dir as $1. We pass paths
# under $SRC/qdistro/<module>/ instead of the legacy /root/<module>-src/.
log "installing Python modules..."
cd "$SRC/qdistro"
QD="$SRC/qdistro"
INSTALLERS=(
    "scripts/install/install-broker-for-qdwin.sh       $QD/broker"
    "scripts/install/install-polkit-agent-for-vm.sh    $QD/polkit"
    "scripts/install/install-pwd-for-vm.sh             $QD/pwd"
    "scripts/install/install-qsu-for-vm.sh             $QD/qsu"
    "scripts/install/install-browser-bridge-for-vm.sh  $QD/browser_bridge"
    "scripts/install/install-phone-for-vm.sh           $QD/phone"
    "scripts/install/install-print-proxy-for-vm.sh     $QD/print"
    "scripts/install/install-recall-for-vm.sh          $QD"
    "scripts/install/install-snapshots-for-vm.sh       $QD/snapshots"
)
for entry in "${INSTALLERS[@]}"; do
    set -- $entry
    installer="$1"
    src_dir="$2"
    if [ -x "$installer" ]; then
        log "  running $(basename "$installer") <- $src_dir"
        bash "$installer" "$src_dir" || { echo "[bootstrap] $installer failed"; exit 3; }
    fi
done

# ---- 5. Install SELinux policy modules (permissive by default) ----------
log "installing SELinux policy modules (permissive)..."
for pol in selinux/broker selinux/pwd selinux/tier1; do
    if [ -d "$pol" ] && [ -x "$pol/install-policy.sh" ]; then
        (cd "$pol" && bash install-policy.sh) || log "  WARN: $pol install failed"
    fi
done

# ---- 6. Install qdwin session (weston + qdshell user units) -------------
log "installing qdwin session (noctalia-session + noctalia-shell user units)..."
bash "$SRC/qdistro/scripts/install/install-qdwin-session-for-vm.sh" \
    "$SRC/qdshell" \
    || { echo "[bootstrap] qdwin-session install failed"; exit 3; }

log "bootstrap complete."
log "start the session now with:"
log "  runuser -l admin -c 'systemctl --user start noctalia-shell.service'"
