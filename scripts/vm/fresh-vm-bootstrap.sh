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
#   - meson + ninja + gcc/cc + libweston-16-devel + wayland-protocols
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

# This bootstrap creates disposable integration-test VMs. The shared profile
# contract keeps direct Tier-1/Tier-2 launches dev-only, while real installs
# default to daily-driver via qdistro-bootstrap.sh.
export QDISTRO_PROFILE="${QDISTRO_PROFILE:-dev}"
case "$QDISTRO_PROFILE" in
    dev|daily-driver|release) ;;
    prod|production) QDISTRO_PROFILE=release ;;
    daily|dd) QDISTRO_PROFILE=daily-driver ;;
    *) echo "[bootstrap] invalid QDISTRO_PROFILE=$QDISTRO_PROFILE" >&2; exit 2 ;;
esac

HOST="${QDISTRO_HTTP_HOST:-http://10.0.2.2:8765}"
SRC=/root/qdistro-src

log() { echo "[bootstrap] $*"; }

install -d -o root -g root -m 0755 /etc/qdistro
cat > /etc/qdistro/profile <<EOF
QDISTRO_PROFILE=$QDISTRO_PROFILE
EOF
chmod 0644 /etc/qdistro/profile

# ---- 0. Defensive masking ------------------------------------------------
# jeos-firstboot fights us for tty1 and blocks multi-user.target;
# greetd would grab the DRM seat and prevent admin's user manager from
# starting qdwin-compositor.service ("Device or resource busy" from
# libseat). build-baked-baseweed.sh masks both at bake time; this is
# belt-and-braces for images baked before that fix.
log "masking jeos-firstboot + greetd (idempotent)..."
systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true
systemctl disable --now greetd.service 2>/dev/null || true
systemctl mask greetd.service 2>/dev/null || true

# ---- 1. Fetch + unpack the three repos -----------------------------------
log "fetching tarballs from $HOST..."
mkdir -p "$SRC"/{qdistro,qdwin,qdshell,qdlocker,qdbrowser,qdgreeter,qnotebook}
for repo in qdistro qdwin qdshell qdlocker qdbrowser qdgreeter qnotebook; do
    if ! wget -q -O "/tmp/$repo.tar.gz" "$HOST/$repo.tar.gz"; then
        # qdlocker + qdbrowser + qdgreeter + qnotebook are optional during the rollout; older
        # spin scripts don't stage them. Don't fail the whole bootstrap
        # if they're absent — the bridge installer will WARN and the
        # qdbrowser pwd_autofill probes will then ModuleNotFoundError,
        # but the broker / pwd / session-manager paths still come up.
        if [ "$repo" = "qdlocker" ] || [ "$repo" = "qdbrowser" ] || [ "$repo" = "qdgreeter" ] || [ "$repo" = "qnotebook" ]; then
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
    PY_PKG_PREFIX=$(python3 - <<'PY'
import sys
print(f"python{sys.version_info.major}{sys.version_info.minor}")
PY
)
    zypper -n --no-gpg-checks refresh >/tmp/qnotebook-zypper-refresh.log 2>&1 \
        || log "  WARN: zypper refresh before qnotebook deps failed; trying cached metadata"
    QNOTEBOOK_ZYPPER_LOG=/tmp/qnotebook-zypper-install.log
    if ! zypper -n install --no-recommends \
            "$PY_PKG_PREFIX-PyQt6" "$PY_PKG_PREFIX-mistune" git \
            >"$QNOTEBOOK_ZYPPER_LOG" 2>&1; then
        log "  ERROR: zypper install of qnotebook deps failed"
        tail -80 "$QNOTEBOOK_ZYPPER_LOG" | sed 's/^/[bootstrap]   zypper: /'
        exit 3
    fi
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
# QDWIN_EXTRA_MESON_OPTS: optional space-separated extra `meson setup` flags
# (e.g. -Denable_test_place=true for the A1-min straddle test build). Empty in
# production spins — the test-only hook is compiled out by default (impl-6 M7).
meson setup build --wipe --prefix=/usr ${QDWIN_EXTRA_MESON_OPTS:-}
meson compile -C build
meson install -C build

# The shared base image can predate new vendored-libweston build dependencies.
# Install the shippable production-profile deps here as a no-op on current bases
# so per-run goldens do not silently degrade when an older baseweed-baked qcow2
# is reused.
log "ensuring vendored libweston production build deps..."
zypper -n install --no-recommends \
    Mesa-libEGL-devel Mesa-libGLESv2-devel Mesa-libGLESv3-devel \
    libdisplay-info-devel libX11-devel libxcb-devel \
    cairo-devel libpng16-devel libpng16-compat-devel pango-devel \
    fontconfig-devel glib2-devel libva-devel liblcms2-devel \
    || { log "  ERROR: zypper install of vendored libweston deps failed"; exit 3; }

# ---- 2b. Build + stage vendored, patched libweston-16 -------------------
# qdwin's layer-shell popup parenting needs soft-linked helper symbols
# that only exist in the patched tree; stock libweston-16 cannot drive
# the get_popup / layer-popup-grab paths. Build the production profile
# and stage it under /usr/libexec/qdistro/qdwin-libweston/ — the qdwin
# systemd unit (written by install-qdwin-session-for-vm.sh below) points
# LD_LIBRARY_PATH + WESTON_MODULE_MAP at that tree.
# Decision doc: qdwin/doc/decisions/0001-vendored-libweston-packaging.md
log "building + staging vendored libweston (production profile)..."
if [ ! -x "$SRC/qdistro/scripts/install/install-vendored-libweston.sh" ]; then
    log "  ERROR: install-vendored-libweston.sh missing — cannot stage vendored libweston"
    exit 3
fi
if ! bash "$SRC/qdistro/scripts/install/install-vendored-libweston.sh" "$SRC/qdwin"; then
    log "  ERROR: vendored libweston staging failed — qdwin must not fall back to distro libweston in CI"
    exit 3
fi
if [ ! -f /usr/libexec/qdistro/qdwin-libweston/lib64/libweston-16/drm-backend.so ]; then
    log "  ERROR: staged vendored libweston missing drm-backend.so"
    exit 3
fi

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
    "scripts/install/install-sdk-for-vm.sh             $QD/sdk/qdistro_app"
    "scripts/install/install-broker-for-qdwin.sh       $QD/broker"
    "scripts/install/install-session-manager.sh        $QD/session_manager"
    "scripts/install/install-user-relay-for-vm.sh      $QD/user_relay"
    "scripts/install/install-polkit-agent-for-vm.sh    $QD/polkit"
    "scripts/install/install-pwd-for-vm.sh             $QD/pwd"
    "scripts/install/install-qsu-for-vm.sh             $QD/qsu"
    "scripts/install/install-media-for-vm.sh           $QD/media"
    "scripts/install/install-multimachine-for-vm.sh    $QD/multimachine"
    "scripts/install/install-browser-bridge-for-vm.sh  $QD/browser_bridge  $SRC/qdbrowser/qdbrowser"
    "scripts/install/install-portal-backend-for-vm.sh  $QD"
    "scripts/install/install-phone-for-vm.sh           $QD/phone"
    "scripts/install/install-print-proxy-for-vm.sh     $QD/print"
    "scripts/install/install-snapshots-for-vm.sh       $QD/snapshots"
    "scripts/install/install-templates-for-vm.sh       $QD"
)
for entry in "${INSTALLERS[@]}"; do
    set -- $entry
    installer="$1"
    src_dir="$2"
    # Run via `bash` so a present-but-non-executable installer still runs — the
    # executable bit must NOT be load-bearing bootstrap control flow. A
    # `[ -x "$installer" ]` gate here once silently skipped the whole
    # template/promotion slice (install-templates-for-vm.sh was committed 0644),
    # so fresh VMs had no template CLIs yet bootstrap stayed green and the
    # in-VM template suites could not have been validly passing. A MISSING
    # installer is a hard error, never a silent skip.
    if [ ! -f "$installer" ]; then
        echo "[bootstrap] missing installer $installer"; exit 3
    fi
    log "  running $(basename "$installer") <- $src_dir"
    bash "$installer" "$src_dir" || { echo "[bootstrap] $installer failed"; exit 3; }
done

# qdistro-approvals is the root/admin CLI used by the permissions GUI
# scenarios and by operators over SSH. spin-test-vm-gui.sh staged it directly,
# but the fresh-bootstrap path used by qci goldens did not, so admin-profile
# scenarios could boot a VM with the broker installed but no CLI.
install -m 0755 "$QD/cli/qdistro_approvals.py" /usr/local/sbin/qdistro-approvals

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
        # Cert dir 0700 (private-key directory must not be group/world
        # traversable); the private key itself is forced to 0600 below.
        install -d -o admin -g admin -m 0700 /home/admin/qdwin-rdp
        runuser -u admin -- winpr-makecert -rdp -path /home/admin/qdwin-rdp \
            >/dev/null 2>&1 || log "  WARN: winpr-makecert failed"
        # winpr-makecert names files <hostname>.{crt,key}; rename.
        (cd /home/admin/qdwin-rdp \
            && for f in *.crt; do [ "$f" = rdp.crt ] && continue; \
                 mv "$f" rdp.crt 2>/dev/null; break; done \
            && for f in *.key; do [ "$f" = rdp.key ] && continue; \
                 mv "$f" rdp.key 2>/dev/null; break; done)
        chown -R admin:admin /home/admin/qdwin-rdp 2>/dev/null || true
        # Lock down: dir 0700, private key 0600, cert 0644 (public).
        chmod 0700 /home/admin/qdwin-rdp 2>/dev/null || true
        [ -f /home/admin/qdwin-rdp/rdp.key ] && chmod 0600 /home/admin/qdwin-rdp/rdp.key 2>/dev/null || true
        [ -f /home/admin/qdwin-rdp/rdp.crt ] && chmod 0644 /home/admin/qdwin-rdp/rdp.crt 2>/dev/null || true
    else
        log "  WARN: winpr-makecert missing — §6.8 nested probes will fail"
    fi
fi

# ---- 5. Install SELinux policy modules (permissive by default) ----------
log "installing SELinux policy modules (permissive)..."
for pol in selinux/broker selinux/pwd selinux/session_manager selinux/tier1; do
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
# qdwin-compositor.service (weston) with:
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

# xwayland provides /usr/bin/Xwayland, which weston's xwayland.so module
# (loaded via the qdwin weston.ini modules= line) execs to serve X11
# clients. The labwc gui profile installs this in spin-test-vm-gui.sh, but
# the qdwin profile builds its own session here, so install it on this path
# too — without it qdwin starts with no working XWayland and every X11 app
# test fails.
log "installing xwayland (Xwayland binary for qdwin's xwayland.so module)..."
zypper -n install --no-recommends xwayland >/dev/null 2>&1 \
    || { log "  ERROR: zypper install xwayland failed"; exit 3; }

# ---- GUI app-deps lane (qdwin XWayland/Wayland app tests) — OPT-IN ---------
# The qdwin app tests (qdwin/tests/apps/*.md) drive real desktop apps —
# firefox, xterm, foot, thunar, vlc, chromium, audacity, feh, tk/fltk/swing.
# OPT-IN (QDWIN_APP_DEPS=1), DEFAULT OFF: this bootstrap is shared by EVERY
# golden (bats, gui-admin, gui-qdwin), so installing these heavy packages —
# and especially the fonts below — unconditionally bloated all goldens and
# shifted the qdshell bar layout enough to break compositor-shell.bats'
# rocket-icon click-coords ("foot never launched"). Only the app-test lane
# needs them, so it must opt in (e.g. a dedicated `QDWIN_APP_DEPS=1 qci gui`
# run); the default full run stays lean and stable. With deps absent the app
# tests are infra-blocked, exactly as before this lane existed.
#
# Best-effort PER PACKAGE: an unavailable/renamed package is logged and
# skipped, NEVER fatal. A wrong name here must never abort the golden build —
# that fail-closed-on-a-missing-dep regression is exactly what we avoid. A
# package that fails to install just leaves its one app test infra-blocked.
if [ "${QDWIN_APP_DEPS:-0}" = 1 ]; then
    log "installing qdwin app-test deps (best-effort; QDWIN_APP_DEPS=0 to skip)..."
    # Map: firefox=MozillaFirefox, gtk4=gnome-text-editor, gtk3=thunar.
    # Thunar's expected Recent/Trash/Computer/Network locations are provided
    # by gvfs + gvfs-backends; installing thunar alone leaves a misleadingly
    # functional but incomplete file-manager test surface.
    # qt5=vlc, electron=chromium, wxwidgets=audacity, tk=python3-tk,
    # fltk demo needs fltk-devel+gcc-c++, swing=java(jdk for javac), imlib2=feh.
    # Fonts: xterm's `-fa Monospace` (Xft) and most toolkits need a real font
    # backing fontconfig's Monospace/Sans aliases — without dejavu/liberation
    # the image has no scalable Monospace and xterm refuses to start.
    _app_pkgs="MozillaFirefox xterm foot gnome-text-editor thunar gvfs gvfs-backends vlc chromium \
audacity python3-tk fltk fltk-devel gcc-c++ feh \
java-21-openjdk java-21-openjdk-devel java-17-openjdk java-17-openjdk-devel \
dejavu-fonts liberation-fonts"
    _app_ok=0; _app_fail=""
    for _pkg in $_app_pkgs; do
        if zypper -n install --no-recommends "$_pkg" >/dev/null 2>&1; then
            _app_ok=$((_app_ok + 1))
        else
            _app_fail="$_app_fail $_pkg"
        fi
    done
    log "  app-deps: $_app_ok package(s) installed;${_app_fail:+ failed:$_app_fail}"
    [ -n "$_app_fail" ] && log "  (failed packages leave their app test infra-blocked, not the build)"
else
    log "skipping qdwin app-test deps (QDWIN_APP_DEPS=0)"
fi

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

# ---- 5d. Configure VM-test synthetic input ------------------------------
# ydotool is test infrastructure only. It needs /dev/uinput, provided by the
# `uinput` kernel module (CONFIG_INPUT_UINPUT=m).
#
# The baseweed template derives from the upstream openSUSE Tumbleweed
# Minimal-VM Cloud image, which ships `kernel-default-base` — a stripped
# kernel package that deliberately omits less-common modules, including
# uinput. (There is no "custom pinned kernel"; the production image at
# image/config.xml already uses the full `kernel-default`, which is why
# production has uinput and only the Minimal-VM-derived test base lacks it.)
#
# Primary fix: `kernel-default` is now in install-deps.sh's package list, so
# a freshly BAKED baseweed (build-baked-baseweed.sh) already carries the full
# kernel and BOOTS it — clones then have uinput from first boot, no reboot.
#
# This block is the FALLBACK for VMs cloned from an OLDER baked image that
# predates that change (uinput modules absent for the running kernel). It
# pulls `kernel-default` from the repo. Tumbleweed rolls forward, so the
# repo's `kernel-default` is usually NEWER than the booted kernel-default-base
# and installs as a *new* kernel under /usr/lib/modules/<newver>/ — uinput is
# then only available after a reboot into that kernel, which we cannot safely
# do mid-bootstrap. In that case we surface a clear WARN; the durable fix is
# to rebake the base. (If the repo still has the matching version, the module
# overlays the running kernel's tree and modprobe below succeeds immediately.)
log "configuring ydotool/uinput for VM GUI tests..."
krel="$(uname -r)"
moddir="/lib/modules/$krel/kernel/drivers/input/misc"
if [ ! -e "$moddir/uinput.ko" ] && [ ! -e "$moddir/uinput.ko.zst" ] \
   && [ ! -e "$moddir/uinput.ko.xz" ]; then
    log "  uinput.ko absent for running kernel ${krel}; installing kernel-default (fallback)..."
    zypper -n --no-gpg-checks refresh >/dev/null 2>&1 || true
    if zypper -n install --no-recommends kernel-default >/dev/null 2>&1; then
        # Refresh the running kernel's module dep index in case the install
        # overlaid a matching version (same-version repo case).
        depmod -a "$krel" >/dev/null 2>&1 || true
        if [ ! -e "$moddir/uinput.ko" ] && [ ! -e "$moddir/uinput.ko.zst" ] \
           && [ ! -e "$moddir/uinput.ko.xz" ]; then
            newk="$(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' kernel-default 2>/dev/null | tail -n1)"
            log "  WARN: kernel-default (${newk:-installed}) provides uinput only for a"
            log "        kernel newer than the booted ${krel}. uinput needs a reboot into"
            log "        the new kernel; ydotoold stays inactive this boot. Rebake the"
            log "        baseweed base (kernel-default is in install-deps.sh) to fix durably."
        fi
    else
        log "  WARN: kernel-default install failed; uinput will be unavailable"
    fi
fi
install -d -m 0755 /etc/udev/rules.d /etc/modules-load.d
cat > /etc/udev/rules.d/60-uinput.rules <<'EOF'
KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
EOF
cat > /etc/modules-load.d/uinput.conf <<'EOF'
uinput
EOF
modprobe uinput >/dev/null 2>&1 || log "  WARN: uinput module unavailable; ydotoold will stay inactive"

# ---- 6. Install qdwin session (weston + qdshell user units) -------------
log "installing qdwin session (qdwin-compositor + qdshell user units via qdwin-session.target)..."
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
Environment=QDLOCKER_PAM_SERVICE=qdlocker
EOF

    # Dedicated screen-unlock PAM service (harden-qdlocker 01+03): explicit
    # pam_faillock brute-force lockout, decoupled from the `login` stack.
    if [ -f "$SRC/qdlocker/pam/qdlocker" ]; then
        install -m 0644 -o root -g root "$SRC/qdlocker/pam/qdlocker" /etc/pam.d/qdlocker
        log "  installed /etc/pam.d/qdlocker (dedicated unlock PAM + faillock lockout)"
    fi

    # Test-only fake fprintd used by qdlocker/tests/gui/02. The helper is
    # staged but not enabled because it owns the same system-bus name as real
    # fprintd; the scenario starts it explicitly after stopping fprintd.
    install -d -m 0755 /usr/libexec /etc/systemd/system /etc/dbus-1/system.d
    cat >/usr/libexec/qdistro-fprintd-fake <<'FAKE'
#!/usr/bin/env python3
import asyncio
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method, signal


class Manager(ServiceInterface):
    def __init__(self):
        super().__init__("net.reactivated.Fprint.Manager")

    @method()
    def GetDefaultDevice(self) -> "o":
        return "/net/reactivated/Fprint/Device/0"


class Device(ServiceInterface):
    def __init__(self):
        super().__init__("net.reactivated.Fprint.Device")

    @method()
    def Claim(self, username: "s"): pass

    @method()
    def Release(self): pass

    @method()
    def VerifyStart(self, finger: "s"): pass

    @method()
    def VerifyStop(self): pass

    @signal()
    def VerifyStatus(self, result: "s", done: "b") -> "sb":
        return [result, done]


class Fake(ServiceInterface):
    def __init__(self, device):
        super().__init__("qdistro.FprintFake")
        self._device = device

    @method()
    def EmitMatch(self):
        self._device.VerifyStatus("verify-match", True)


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    device = Device()
    bus.export("/net/reactivated/Fprint/Manager", Manager())
    bus.export("/net/reactivated/Fprint/Device/0", device)
    bus.export("/net/reactivated/Fprint/Device/0", Fake(device))
    await bus.request_name("net.reactivated.Fprint")
    await asyncio.Event().wait()


asyncio.run(main())
FAKE
    chmod 0755 /usr/libexec/qdistro-fprintd-fake
    cat >/etc/systemd/system/qdistro-fprintd-fake.service <<'UNIT'
[Unit]
Description=qdistro fake fprintd for VM tests

[Service]
Type=dbus
BusName=net.reactivated.Fprint
ExecStart=/usr/libexec/qdistro-fprintd-fake

[Install]
WantedBy=multi-user.target
UNIT
    cat >/etc/dbus-1/system.d/qdistro-fprintd-fake.conf <<'DBUS'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="net.reactivated.Fprint"/>
    <allow send_destination="net.reactivated.Fprint"/>
  </policy>
  <policy user="admin">
    <allow send_destination="net.reactivated.Fprint"
           send_interface="qdistro.FprintFake"/>
    <allow send_destination="net.reactivated.Fprint"
           send_interface="net.reactivated.Fprint.Manager"/>
    <allow send_destination="net.reactivated.Fprint"
           send_interface="net.reactivated.Fprint.Device"/>
  </policy>
</busconfig>
DBUS
    systemctl daemon-reload || true
    systemctl reload dbus.service 2>/dev/null || systemctl reload dbus-broker.service 2>/dev/null || true
    log "  installed qdistro-fprintd-fake test service"

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
# start qdwin-session.target (which pulls qdwin-compositor.service +
# qdshell.service in via Requires=/Wants=). qdlocker.service was enabled
# in §7 and starts via default.target once the user manager comes up.
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
runuser -l admin -c 'systemctl --user start qdwin-session.target' || true
runuser -l admin -c 'systemctl --user start ydotoold.service' \
    || log "  WARN: ydotoold.service did not start (expected if /dev/uinput is absent)"

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
    runuser -l admin -c 'journalctl --user -u qdwin-compositor.service --no-pager -n 30' || true
fi

# qdlocker.sock is created by a successfully running qdlocker by default
# (QDLOCKER_CTRL_SOCKET=1). This admin-test harness starts the lxqt/labwc
# path, not the qdwin compositor session, so qdlocker may retry-crash before
# binding qdwin_locker_v1 and before creating the ctrl socket. The
# /etc/qdistro/locker-ctrl-introspection marker gates only the diagnostic
# commands (status/unlock-result/prompt-text); it does not gate socket
# creation or the production `lock` command. Warn-and-continue — do NOT fail
# the bootstrap on this harness; only flag the case that signals a real
# regression (qdlocker active but no socket).
log "  waiting for /run/user/1000/qdlocker.sock (best-effort)..."
for _ in $(seq 1 30); do
    [ -S /run/user/1000/qdlocker.sock ] && break
    sleep 1
done
if [ ! -S /run/user/1000/qdlocker.sock ]; then
    # is-active returns nonzero for an inactive/failed unit; neutralize so the
    # substitution doesn't trip set -e/pipefail before we classify the state.
    locker_state=$(runuser -l admin -c 'systemctl --user is-active qdlocker.service' 2>/dev/null | tr -d '\r' || true)
    if [ "${locker_state:-}" = "active" ]; then
        log "  WARN: /run/user/1000/qdlocker.sock missing though qdlocker.service is active — possible ctrl-socket regression (not expected absence)"
    else
        log "  WARN: /run/user/1000/qdlocker.sock absent (expected on this harness: qdlocker.service is '${locker_state:-unknown}', no qdwin compositor session; marker gates introspection commands only)"
    fi
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
            || log "  WARN: tier-4 base build failed; phase7-tier4-vm will SKIP"
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

# Pre-build the tier-2 podman workload images into admin's rootless store so the
# tier-2 bats drivers (s32/s40/s33/s34/s59, wlimg-e2e) don't pay a cold
# `podman build` — a Tumbleweed pull + zypper install, ~5-6 min wall under CI
# contention — inside their readiness hot path (the #1 cause of tiered-isolation
# / tier2-hardening-lockin flakiness). Built AS admin (uid 1000) to match the
# drivers' `runuser -u admin -- podman` rootless store. Opt-in: the bats per-run
# golden sets the gate, so every cloned worker inherits the images as shared CoW
# backing blocks. Failure here is FATAL when opted in — silently falling back to
# the per-worker on-demand build is exactly the flaky path this removes.
if [ "${QDISTRO_BUILD_TIER2_IMAGES:-0}" = "1" ]; then
    if [ ! -x "$SRC/qdistro/tier2/make-tier2-image.sh" ]; then
        log "  ERROR: tier2/make-tier2-image.sh not staged; cannot pre-build tier-2 images"
        exit 1
    fi
    log "pre-building tier-2 podman images (QDISTRO_BUILD_TIER2_IMAGES=1)..."
    if ! runuser -u admin -- bash "$SRC/qdistro/tier2/make-tier2-image.sh"; then
        log "  ERROR: tier-2 image pre-build failed"
        exit 1
    fi
    # Verify each expected tag actually landed in admin's store. A partial build
    # (script exits 0 but one tag missing) would silently leave the on-demand
    # path for that workload.
    for _w in weston-terminal text-viewer url-preview; do
        if ! runuser -u admin -- podman image exists "qdistro/tier2-${_w}:latest"; then
            log "  ERROR: expected image qdistro/tier2-${_w}:latest missing after pre-build"
            exit 1
        fi
    done
    log "  tier-2 images pre-built: weston-terminal, text-viewer, url-preview"
fi

log "bootstrap complete."
log "session was started by §7d; if it failed, restart with:"
log "  runuser -l admin -c 'systemctl --user restart qdwin-session.target'"
log "  runuser -l admin -c 'systemctl --user restart qdlocker.service'"
