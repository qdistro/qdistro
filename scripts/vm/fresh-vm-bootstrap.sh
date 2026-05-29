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
#   - meson + ninja + gcc/cc + libweston-14-devel + wayland-protocols
#     (gcc is required: install-qsu-for-vm.sh compiles qsu.c into the
#      /usr/local/bin/qsu ELF binary so /proc/<pid>/exe is unambiguous)
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
mkdir -p "$SRC"/{qdistro,qdwin,qdshell,qdlocker,qdbrowser,qnotebook}
for repo in qdistro qdwin qdshell qdlocker qdbrowser qnotebook; do
    if ! wget -q -O "/tmp/$repo.tar.gz" "$HOST/$repo.tar.gz"; then
        # qdlocker + qdbrowser + qnotebook are optional during the rollout; older
        # spin scripts don't stage them. Don't fail the whole bootstrap
        # if they're absent — the bridge installer will WARN and the
        # qdbrowser pwd_autofill probes will then ModuleNotFoundError,
        # but the broker / pwd / session-manager paths still come up.
        if [ "$repo" = "qdlocker" ] || [ "$repo" = "qdbrowser" ] || [ "$repo" = "qnotebook" ]; then
            log "$repo tarball not staged; skipping"
            rmdir "$SRC/$repo" 2>/dev/null || true
            continue
        fi
        echo "[bootstrap] failed to fetch $HOST/$repo.tar.gz"; exit 2
    fi
    tar -xzf "/tmp/$repo.tar.gz" -C "$SRC/$repo"
    rm -f "/tmp/$repo.tar.gz"
done

if [ -f "$SRC/qnotebook/pyproject.toml" ]; then
    log "installing qnotebook..."
    zypper -n install --no-recommends python313-PyQt6 python313-mistune git \
        >/dev/null 2>&1 || \
        { log "  ERROR: zypper install of qnotebook deps failed"; exit 3; }
    PY_SITE=$(python3 - <<'PY'
import sysconfig
print(sysconfig.get_paths()["purelib"].replace("/usr/lib/", "/usr/local/lib/", 1))
PY
)
    rm -rf "$PY_SITE/qnotebook"
    install -d -m 0755 "$PY_SITE"
    cp -a "$SRC/qnotebook/qnotebook" "$PY_SITE/qnotebook"
    cat > /usr/local/bin/qnotebook <<'EOF'
#!/bin/sh
exec python3 -m qnotebook "$@"
EOF
    chmod 0755 /usr/local/bin/qnotebook
fi

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
    "scripts/install/install-user-relay-for-vm.sh      $QD/user_relay"
    "scripts/install/install-polkit-agent-for-vm.sh    $QD/polkit"
    "scripts/install/install-pwd-for-vm.sh             $QD/pwd"
    "scripts/install/install-qsu-for-vm.sh             $QD/qsu"
    "scripts/install/install-browser-bridge-for-vm.sh  $QD/browser_bridge  $SRC/qdbrowser/qdbrowser"
    "scripts/install/install-portal-backend-for-vm.sh  $QD"
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
    # .sh probes are exec'd; .py / .c siblings are read by the probes.
    for probe in "$PROBE_SRC"/*.sh; do
        [ -e "$probe" ] || continue
        install -m 0755 "$probe" "/root/$(basename "$probe")"
    done
    for aux in "$PROBE_SRC"/*.py "$PROBE_SRC"/*.c; do
        [ -e "$aux" ] || continue
        install -m 0644 "$aux" "/root/$(basename "$aux")"
    done
fi

# ---- 4c. Generate RDP TLS cert/key for §6.8 nested probes ---------------
# The s21/s23/s25/s28 nested probes start weston with the RDP backend,
# which requires /home/admin/qdwin-rdp/{rdp.crt,rdp.key}. winpr-makecert
# emits these idempotently; without them the RDP listener refuses
# incoming peers ("BIO_new failed for certificate") and probes time
# out waiting for nested_manager binds and seat creation.
if [ ! -f /home/admin/qdwin-rdp/rdp.crt ] || [ ! -f /home/admin/qdwin-rdp/rdp.key ]; then
    if command -v winpr-makecert >/dev/null 2>&1; then
        log "generating RDP TLS cert/key (winpr-makecert)..."
        install -d -o admin -g admin /home/admin/qdwin-rdp
        runuser -u admin -- winpr-makecert -rdp -path /home/admin/qdwin-rdp \
            >/dev/null 2>&1 || log "  WARN: winpr-makecert failed"
        # winpr-makecert names files <hostname>.{crt,key}; rename.
        (cd /home/admin/qdwin-rdp \
            && for f in *.crt; do [ "$f" = rdp.crt ] && continue; \
                 mv "$f" rdp.crt 2>/dev/null; break; done \
            && for f in *.key; do [ "$f" = rdp.key ] && continue; \
                 mv "$f" rdp.key 2>/dev/null; break; done)
        chown -R admin:admin /home/admin/qdwin-rdp 2>/dev/null || true
    else
        log "  WARN: winpr-makecert missing — §6.8 nested probes will fail"
    fi
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

# ---- 5c. Install + enable seatd (system service) ------------------------
# admin's lingering user@1000.service is a "manager" session that does
# NOT own a logind seat, so libseat's logind backend fails inside
# noctalia-session.service (weston) with:
#   libseat/backend/logind.c: Could not get primary session for user
#   libseat/backend/seatd.c:  Could not connect to socket /run/seatd.sock
#   libseat: could not open seat
#   fatal: failed to create compositor backend
# Running seatd as a system service exposes /run/seatd.sock (root:seat
# 0660) so the seatd backend works for any user in the `seat` group.
# install-qdwin-session-for-vm.sh adds admin to that group.
log "installing + enabling seatd (system service)..."
zypper -n install --no-recommends seatd >/dev/null 2>&1 \
    || { log "  ERROR: zypper install seatd failed"; exit 3; }
groupadd -f seat
cat > /etc/systemd/system/seatd.service <<'EOF'
[Unit]
Description=Seat management daemon
Documentation=man:seatd(1)
After=systemd-user-sessions.service
Before=user@.service

[Service]
Type=simple
ExecStart=/usr/bin/seatd -g seat
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now seatd.service \
    || { log "  ERROR: failed to enable+start seatd.service"; exit 3; }
for i in 1 2 3 4 5; do
    [ -S /run/seatd.sock ] && break
    sleep 1
done
if [ ! -S /run/seatd.sock ]; then
    log "  WARN: /run/seatd.sock did not appear within 5s"
fi

# ---- 6. Install qdwin session (weston + qdshell user units) -------------
log "installing qdwin session (noctalia-session + noctalia-shell user units)..."
bash "$SRC/qdistro/scripts/install/install-qdwin-session-for-vm.sh" \
    "$SRC/qdshell" \
    || { echo "[bootstrap] qdwin-session install failed"; exit 3; }

# qdwin does not own cursor image buffers itself. Keep the helper alive
# as part of the qdshell session so it can register wl_shm cursor
# surfaces through qdwin_shell_v1.set_cursor_sprite.
if [ -f "$SRC/qdistro/daemons/cursor-sprites/qdistro-cursor-sprites.service" ]; then
    log "installing qdwin cursor sprite helper user unit..."
    install -d -m 0755 /etc/systemd/user
    install -m 0644 \
        "$SRC/qdistro/daemons/cursor-sprites/qdistro-cursor-sprites.service" \
        /etc/systemd/user/qdistro-cursor-sprites.service
    runuser -l admin -c \
        'systemctl --user enable qdistro-cursor-sprites.service' \
        || log "  WARN: qdistro-cursor-sprites.service enable failed"
fi

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
        python313-pip python313-PyQt6 python313-python-pam \
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
Environment=QDLOCKER_PAM_SERVICE=login
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

# ---- 7d. Start the user session ----------------------------------------
# install-qdwin-session-for-vm.sh adds admin to the `seat` group; the
# new group membership only takes effect on a fresh session, so we
# terminate any existing user manager and re-enable linger to get a
# clean user@1000.service with the right supplementary groups, then
# start noctalia-shell.service (which pulls noctalia-session.service
# in via Requires=). qdlocker.service was enabled in §7 and starts via
# default.target once the user manager comes up.
log "starting admin user session..."
loginctl terminate-user admin 2>/dev/null || true
# Wait for the user manager to actually go away before re-lingering.
for i in 1 2 3 4 5; do
    systemctl is-active --quiet user@1000.service || break
    sleep 1
done
loginctl enable-linger admin
for i in 1 2 3 4 5; do
    systemctl is-active --quiet user@1000.service && break
    sleep 1
done
if ! systemctl is-active --quiet user@1000.service; then
    log "  WARN: user@1000.service did not become active within 5s; user-session start may fail"
fi

runuser -l admin -c 'systemctl --user daemon-reload' || true
runuser -l admin -c 'systemctl --user start noctalia-shell.service' || true

log "  enabling pipewire user socket..."
runuser -l admin -c 'systemctl --user enable --now pipewire.socket pipewire.service' \
    || log "  WARN: pipewire enable failed"

log "  waiting for /run/user/1000/wayland-1..."
for i in $(seq 1 30); do
    [ -S /run/user/1000/wayland-1 ] && break
    sleep 1
done
if [ ! -S /run/user/1000/wayland-1 ]; then
    log "  WARN: /run/user/1000/wayland-1 did not appear within 30s"
    log "  weston logs:"
    runuser -l admin -c 'journalctl --user -u noctalia-session.service --no-pager -n 30' || true
fi

# qdlocker has a known WAYLAND_DISPLAY env-propagation bug being fixed
# in the qdlocker repo by another agent; the socket may not appear yet.
# Warn-and-continue — do NOT fail the bootstrap on this.
log "  waiting for /run/user/1000/qdlocker.sock (best-effort)..."
for i in $(seq 1 30); do
    [ -S /run/user/1000/qdlocker.sock ] && break
    sleep 1
done
if [ ! -S /run/user/1000/qdlocker.sock ]; then
    log "  WARN: /run/user/1000/qdlocker.sock did not appear within 30s (known qdlocker env bug)"
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
log "session was started by §7d; if it failed, restart with:"
log "  runuser -l admin -c 'systemctl --user restart noctalia-shell.service'"
log "  runuser -l admin -c 'systemctl --user restart qdlocker.service'"
