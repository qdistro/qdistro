#!/bin/bash
# fresh-vm-bootstrap.sh — run inside a freshly-cloned baseweed VM to:
#   1. Fetch the three qdistro repos as tarballs from host:8765.
#   2. Build qdwin from source (libweston shell plugin).
#   3. Build qdistro's C daemons against qdwin's protocol XML.
#   4. Install the Python broker / polkit-agent / pwd / etc. services.
#   5. Install the qdshell QML stack.
#   6. Install + enable user systemd units that start qdwin + qdshell.
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

# ---- 0. Defensive masking ------------------------------------------------
# jeos-firstboot fights us for tty1 and blocks multi-user.target;
# greetd would grab the DRM seat and prevent admin's user manager from
# starting noctalia-session.service ("Device or resource busy" from
# libseat). build-baked-baseweed.sh masks both at bake time; this is
# belt-and-braces for images baked before that fix.
log "masking jeos-firstboot + greetd (idempotent)..."
systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true
systemctl disable --now greetd.service 2>/dev/null || true
systemctl mask greetd.service 2>/dev/null || true

# ---- 1. Fetch + unpack the three repos -----------------------------------
log "fetching tarballs from $HOST..."
mkdir -p "$SRC"/{qdistro,qdwin,qdshell,qdlocker}
for repo in qdistro qdwin qdshell qdlocker; do
    if ! wget -q -O "/tmp/$repo.tar.gz" "$HOST/$repo.tar.gz"; then
        # qdlocker is optional during the rollout; older spin scripts
        # don't stage it. Don't fail the whole bootstrap if it's absent.
        if [ "$repo" = "qdlocker" ]; then
            log "qdlocker tarball not staged; skipping (no peer locker installed)"
            rmdir "$SRC/qdlocker" 2>/dev/null || true
            continue
        fi
        echo "[bootstrap] failed to fetch $HOST/$repo.tar.gz"; exit 2
    fi
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
    "scripts/install/install-session-manager.sh        $QD/session_manager"
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

# ---- 4b. Stage bats in-VM probes at /root/ ------------------------------
# Bats tests in tests/integration/vm/*.bats run `bash
# /root/sNN-foo-probe.sh` to drive end-to-end checks inside the VM.
# Source lives at tests/integration/vm/probes/ in the qdistro umbrella;
# copy them to /root/ so the bats files find them.
PROBE_SRC="$QD/tests/integration/vm/probes"
if [ -d "$PROBE_SRC" ]; then
    log "staging bats probes from $PROBE_SRC -> /root/"
    install -d -m 0755 /root
    for probe in "$PROBE_SRC"/*.sh; do
        [ -e "$probe" ] || continue
        install -m 0755 "$probe" "/root/$(basename "$probe")"
    done
fi

# ---- 5. Install SELinux policy modules (permissive by default) ----------
log "installing SELinux policy modules (permissive)..."
for pol in selinux/broker selinux/pwd selinux/tier1; do
    if [ -d "$pol" ] && [ -x "$pol/install-policy.sh" ]; then
        (cd "$pol" && bash install-policy.sh) || log "  WARN: $pol install failed"
    fi
done

# ---- 5b. Build qdshell QML plugin (libqdistro-qdwin.so) ------------------
# The Qdistro.Qdwin QML plugin lives in qdshell/qml-plugin/ and reads
# the qdwin_shell_v1 protocol XML from the sibling qdwin repo via a
# relative path (../../qdwin/qdwin/qdwin-shell-v1.xml). Both repos are
# unpacked side-by-side under $SRC so the relative path resolves.
# Without this, qdshell's Services/Qdwin/Qdwin.qml cannot resolve
# `import Qdistro.Qdwin 1.0` and `qs` exits with rc=255 on startup.
log "building qdshell QML plugin (libqdistro-qdwin.so)..."
cd "$SRC/qdshell"
meson setup build --wipe --prefix=/usr \
    || { log "  ERROR: qdshell meson setup failed"; exit 3; }
meson compile -C build \
    || { log "  ERROR: qdshell meson compile failed"; exit 3; }
meson install -C build \
    || { log "  ERROR: qdshell meson install failed"; exit 3; }

# ---- 6. Install qdwin session (weston + qdshell user units) -------------
log "installing qdwin session (noctalia-session + noctalia-shell user units)..."
bash "$SRC/qdistro/scripts/install/install-qdwin-session-for-vm.sh" \
    "$SRC/qdshell" \
    || { echo "[bootstrap] qdwin-session install failed"; exit 3; }

# ---- 7. Install qdlocker (peer screen-locker process) -------------------
# Optional: skipped when the qdlocker tarball was not staged (see §1).
#
# Package names verified against openSUSE Tumbleweed Minimal-VM Cloud
# 2026-05-15 during the qdlocker smoke run. The generic `python3-*`
# names don't exist there; the actual packages are `python313-*`.
# pip install of the locker uses `--no-deps` because letting pip
# resolve transitive deps reaches for pywayland-0.5+ wheels that
# fail to build without wayland-devel + gcc — the zypper-shipped
# python313-pywayland 0.4.x is what we want.
if [ -d "$SRC/qdlocker/qdlocker" ]; then
    log "installing qdlocker (Python+QML peer locker via qdwin_locker_v1)..."
    zypper -n install --no-recommends \
        python313-pip python313-pyside6 python313-python-pam \
        python313-dbus_next python313-pywayland \
        >/dev/null 2>&1 || \
        { log "  ERROR: zypper install of qdlocker deps failed"; exit 3; }

    python3 -m pip install --break-system-packages --no-deps --quiet \
        "$SRC/qdlocker" \
        || { log "  ERROR: pip install qdlocker failed"; exit 3; }

    # Install the systemd user unit for admin. The unit's
    # `qdshell-path` drop-in points the QML engine at qdshell's
    # styling (installed by install-qdwin-session-for-vm.sh).
    install -d /home/admin/.config/systemd/user
    install -m 644 "$SRC/qdlocker/systemd/qdlocker.service" \
        /home/admin/.config/systemd/user/qdlocker.service
    chown -R admin:users /home/admin/.config/systemd
    install -d /etc/systemd/user/qdlocker.service.d
    cat > /etc/systemd/user/qdlocker.service.d/qdshell-path.conf <<EOF
[Service]
Environment=QDLOCKER_QDSHELL_PATH=/usr/share/quickshell/qdshell
EOF

    runuser -l admin -c 'systemctl --user daemon-reload' || true
    runuser -l admin -c 'systemctl --user enable qdlocker.service' || true
    log "  qdlocker installed; will start with the user session"
else
    log "qdlocker tarball not present, skipping installation"
fi

# ---- 7b. Install tier-5 launcher infrastructure -------------------------
# Symlinks qdistro-tier5-spawn + cleanup + build-guest-image into
# /usr/local/bin, and installs the polkit policy that lets the active
# admin session pkexec the spawn helper without re-auth. Required for
# qdshell's VMAppsProvider to actually launch tier-5 apps (admin uid
# can't run libvirt/virsh as root without this).
if [ -x "$SRC/qdistro/scripts/install/install-tier5-for-vm.sh" ]; then
    log "installing tier-5 launcher symlinks + polkit policy..."
    bash "$SRC/qdistro/scripts/install/install-tier5-for-vm.sh" \
        "$SRC/qdistro" \
        || log "  WARN: tier-5 launcher install failed; VMAppsProvider will not work"
fi

# ---- 7c. Install tier-3 launcher infrastructure -------------------------
# Creates the qdistro-tier3 group + user1/user2 silo accounts, adds
# admin to the group, symlinks spawn-tier3 + cleanup, and installs the
# polkit policy. Required for the phase7-tier3-* bats family to have
# a populated silo to spawn into.
if [ -x "$SRC/qdistro/scripts/install/install-tier3-for-vm.sh" ]; then
    log "installing tier-3 silo + launcher symlinks + polkit policy..."
    bash "$SRC/qdistro/scripts/install/install-tier3-for-vm.sh" \
        "$SRC/qdistro" \
        || log "  WARN: tier-3 launcher install failed; phase7-tier3-* bats will fail loud"
fi

# ---- 8. Opt-in: build tier-4 / tier-5 guest base images ----------------
# These produce the qcow2 base images that spawn-tier4.sh / spawn-tier5.sh
# linked-clone from. ~400-500 MB upstream Tumbleweed Minimal-VM Cloud
# download per tier + ~30-60s of virt-customize. Opt-in via env vars:
# the phase7-tier{4,5}-* bats SKIPs gracefully when the base disk is
# absent, so this is only needed when the operator wants those tests
# to actually exercise the full --vm path. tiered-isolation.bats's
# error message points at these env vars.
if [ "${QDISTRO_BUILD_TIER4_BASE:-0}" = "1" ]; then
    if [ -x "$SRC/qdistro/tier4-vm/build-guest-image.sh" ]; then
        log "building tier-4 base disk (QDISTRO_BUILD_TIER4_BASE=1)..."
        bash "$SRC/qdistro/tier4-vm/build-guest-image.sh" \
    else
        log "  WARN: tier4-vm/build-guest-image.sh not staged; skipping"
    fi
fi
if [ "${QDISTRO_BUILD_TIER5_BASE:-0}" = "1" ]; then
    if [ -x "$SRC/qdistro/tier5-vm/build-guest-image.sh" ]; then
        log "building tier-5 base disk (QDISTRO_BUILD_TIER5_BASE=1)..."
        bash "$SRC/qdistro/tier5-vm/build-guest-image.sh" \
            || log "  WARN: tier-5 base build failed; phase7-tier5-vm will SKIP"
    else
        log "  WARN: tier5-vm/build-guest-image.sh not staged; skipping"
    fi
fi

log "bootstrap complete."
log "start the session now with:"
log "  runuser -l admin -c 'systemctl --user start noctalia-shell.service'"
log "  runuser -l admin -c 'systemctl --user start qdlocker.service'"
