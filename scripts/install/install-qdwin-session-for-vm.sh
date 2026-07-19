#!/bin/bash
# Install the admin-user systemd session that runs qdwin + qdshell.
#
# Three user units land in /home/admin/.config/systemd/user/, mirroring
# the PRODUCTION deploy contract (qdistro/deploy/) so the GUI test lanes
# validate the units deploy actually ships:
#
#   qdwin-compositor.service  — weston with qdwin-shell.so on the drm
#                               backend (+ pipewire sub-backend for
#                               §6.5 view_stream outputs), claiming
#                               wayland-1.
#   qdshell.service           — quickshell loading the qdshell QML stack
#                               from /usr/share/quickshell/qdshell/.
#   qdwin-session.target      — the session target that pulls in the
#                               compositor (Requires=) + shell (Wants=).
#
# The unit names match deploy exactly (was the legacy noctalia-session /
# noctalia-shell pair, retired 2026-06-16 — deploy-contract drift
# followup). The [Unit] graph mirrors deploy/: the compositor + shell are
# PartOf= the target, the shell After=/Requires= the compositor, and the
# target Requires= the compositor + Wants= the shell.
#
# VM-vs-deploy divergence: this installer ENABLES qdwin-session.target
# under default.target so the lingering admin user-manager auto-starts the
# desktop in the headless/spin-test VM (which has no greeter to start it).
# The production greeter image starts the target EXPLICITLY via
# qdwin-session-launcher after PAM auth and must NOT also enable it under
# default.target (that would race for wayland-1) — image/config.sh removes
# the default.target.wants symlink for exactly that reason.
#
# The [Service] bodies keep the VM-specific tuning the static deploy units
# do not carry: a dynamically computed WESTON_MODULE_MAP (vendored vs
# distro libweston), a conditional LD_LIBRARY_PATH, an explicit
# XDG_RUNTIME_DIR (the lingering user manager path), and the VM weston.ini
# with the pipewire sub-backend + idle-time=0.
#
# Args:
#   $1 — path to qdshell source tree (default /root/qdistro-src/qdshell)
#
# Side effects:
#   - admin added to video / input / render groups (needed for drm
#     backend + libinput).
#   - admin lingering enabled so the user manager runs after
#     autologin completes.
#   - /home/admin/weston.ini written with qdwin-shell.so + drm backend
#     + pipewire sub-backend (num-outputs=2).
#   - qdshell QML copied to /usr/share/quickshell/qdshell/ (system-
#     wide so multiple users could share).
#   - qdshell's compiled QML plugin (libqdistro-qdwin.so + qmldir)
#     copied to /usr/share/qdistro/qml/Qdistro/Qdwin/. Caller is
#     expected to have run `meson compile -C build` under qdshell/
#     so qdshell/build/qml-plugin/libqdistro-qdwin.so exists. Override
#     the source location via $QDSHELL_PLUGIN_BUILD if needed.
#   - Both user units enabled (but not started — caller decides when
#     to start them, typically at next greetd tty3 login).

set -eu

QDSHELL_SRC=${1:-/root/qdistro-src/qdshell}

if [ ! -d "$QDSHELL_SRC" ]; then
    echo "ERROR: qdshell source not found at $QDSHELL_SRC" >&2
    echo "       pass the qdshell/ dir as \$1 or untar qdshell to /root/qdistro-src/qdshell/" >&2
    exit 2
fi

# 1. Groups + linger.
# `seat` is required so libseat's seatd backend can open a seat for
# weston when the compositor runs under admin's lingering user manager
# (which has no logind seat of its own). The group is created by
# fresh-vm-bootstrap.sh's seatd setup step.
usermod -aG video,input,render,seat admin
loginctl enable-linger admin

# 2. weston.ini: qdwin-shell.so + drm backend so the VM console sees
# the framebuffer.
#
# Path resolution: qdwin was built with `meson setup build` (no
# --prefix) which defaults to /usr/local. fresh-vm-bootstrap.sh
# passes --prefix=/usr so qdwin-shell.so lands at /usr/lib64/weston/.
# If you're hand-building elsewhere, adjust this path to match
# `meson install --dry-run` output.
#
# renderer=gl is required for the virtio-gpu hardware cursor plane:
# libweston only allocates the GBM cursor BOs in the GL/EGL path
# (drm-gbm.c), so under pixman b->gbm is NULL and the cursor is always
# software-composited into the scanout — which, under SPICE, doubles
# with the host/client cursor (see todo/issues/qdwin/vm-double-cursor.md).
# GL runs fine on a software-only virtio-gpu via llvmpipe-over-GBM
# (Mesa 26.1.0); the old "GL segfaults on software-only virtio-gpu"
# rationale is stale. Paired with libweston's DRM_CLIENT_CAP_CURSOR_PLANE_HOTSPOT
# support, the cursor then lands on the DRM cursor plane (off the
# scanout) and QEMU forwards it to SPICE → a single cursor.
#
# qdwin's §6.5 view_stream forwarding (qdwin_shell_v1.subscribe_view_stream)
# pins views onto free "pipewire*" compositor outputs, so the pipewire
# backend must actually be LOADED — not merely configured. weston's --backend
# cmdline takes a single plugin name, so we load BOTH backends via the
# `[core] backend=` list (drm first = primary virtio-gpu console; pipewire
# second = off-screen view_stream capture outputs) and drop the --backend
# override from the unit ExecStart so this list takes effect. The `[pipewire]`
# section only *configures* the already-loaded backend (it does not load it);
# num-outputs=2 gives headroom for concurrent forwards.
# idle-time=0 disables weston's built-in idle timer and flips qdwin
# into its internal-idle mode so ext-idle-notify-v1 subscribers
# (qdlocker — see qdlocker/qdlocker/idle.py) receive `idled` events
# at their requested timeout rather than only after weston's 300s
# default. The s103-locker-idle.sh Test 3 sets QDLOCKER_IDLE_MS=2000
# and expects the lock to fire within 10s; that path only works when
# qdwin is running in internal-idle mode.
cat > /home/admin/weston.ini <<'EOF'
[core]
backend=drm-backend.so,pipewire-backend.so
shell=/usr/lib64/weston/qdwin-shell.so
renderer=gl
modules=
xwayland=false
idle-time=0

[shell]
locking=false
client=

[output]
name=Virtual-1
mode=1920x1080@60

[pipewire]
num-outputs=2
EOF
chown admin:users /home/admin/weston.ini

# 3. System-wide qdshell QML at /usr/share/quickshell/qdshell/.
install -d -o root -g root -m 0755 /usr/share/quickshell
rm -rf /usr/share/quickshell/qdshell
cp -r "$QDSHELL_SRC" /usr/share/quickshell/qdshell
chown -R root:root /usr/share/quickshell/qdshell

# 3b. Qdistro.Qdwin QML plugin — qdshell's native binding to
# qdwin_shell_v1. Built from qdshell/qml-plugin/ (which reads the
# protocol XML from the qdwin sibling repo at build time). Without
# this, qdshell's Services/Qdwin/Qdwin.qml cannot resolve
# `import Qdistro.Qdwin 1.0` and falls back to the no-binding stubs.
QDSHELL_PLUGIN_BUILD="${QDSHELL_PLUGIN_BUILD:-$QDSHELL_SRC/build}"
PLUGIN_SO="$QDSHELL_PLUGIN_BUILD/qml-plugin/libqdistro-qdwin.so"
if [ -f "$PLUGIN_SO" ]; then
    # Staleness guard. A plugin .so older than its own sources is the
    # classic cause of the qdshell crash-loop: the .so predates a
    # Q_PROPERTY (e.g. `outputs`) that the deployed Qdwin.qml binds via a
    # NOTIFY handler (onOutputsChanged), so Quickshell throws "Cannot
    # assign to non-existent property", exits 255, and nothing paints.
    # Copying a stale build artifact turns a missed `meson compile` into a
    # silent black screen. Refuse it here so the skew is a loud install-time
    # error instead — rebuild and re-run. (Set QDSHELL_ALLOW_STALE_PLUGIN=1
    # to override, e.g. when intentionally shipping a prebuilt .so.)
    newer_src=$(find "$QDSHELL_SRC/qml-plugin" -type f \
        \( -name '*.cpp' -o -name '*.h' -o -name '*.xml' -o -name 'meson.build' \) \
        -newer "$PLUGIN_SO" -print -quit 2>/dev/null || true)
    if [ -n "$newer_src" ] && [ "${QDSHELL_ALLOW_STALE_PLUGIN:-0}" != 1 ]; then
        echo "ERROR: $PLUGIN_SO is STALE — '$newer_src' is newer than the" >&2
        echo "       built plugin. Shipping it risks the qdshell" >&2
        echo "       onOutputsChanged crash-loop (version-skewed QML <-> plugin)." >&2
        echo "       Rebuild: 'meson compile -C $QDSHELL_PLUGIN_BUILD' then re-run." >&2
        echo "       (override with QDSHELL_ALLOW_STALE_PLUGIN=1)" >&2
        exit 2
    fi
    install -d -o root -g root -m 0755 /usr/share/qdistro/qml/Qdistro/Qdwin
    install -m 0755 -o root -g root \
        "$QDSHELL_PLUGIN_BUILD/qml-plugin/libqdistro-qdwin.so" \
        /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
    install -m 0644 -o root -g root \
        "$QDSHELL_SRC/qml-plugin/qmldir" \
        /usr/share/qdistro/qml/Qdistro/Qdwin/qmldir
    echo "qdwin-shell-v1 QML plugin installed: $(ls -la /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so | awk '{print $5}') bytes"
else
    echo "WARN: $QDSHELL_PLUGIN_BUILD/qml-plugin/libqdistro-qdwin.so not found —" \
         "qdshell will run without qdwin_shell_v1 binding (Qdwin.qml" \
         "import will fail). Rebuild qdshell with 'meson setup build &&" \
         "meson compile -C build' in $QDSHELL_SRC, then re-run this script."
fi

# 3c. Tier-2 host-side spawn helper. PodApps.qml shells out to
# `qdistro-tier2-spawn` on PATH; install spawn-tier2.sh under that
# canonical name so launches from the live shell work without baking
# the dev-tree path in QML. The script is self-contained — entrypoint.sh
# and the rest live inside the container image, not on the host.
QDISTRO_SRC="${QDISTRO_SRC:-/root/qdistro-src/qdistro}"
if [ -f "$QDISTRO_SRC/tier2/spawn-tier2.sh" ]; then
    install -d /usr/lib/qdistro
    install -m 0644 -o root -g root \
        "$QDISTRO_SRC/lib/spawn-common.sh" \
        /usr/lib/qdistro/spawn-common.sh
    install -m 0755 -o root -g root \
        "$QDISTRO_SRC/tier2/spawn-tier2.sh" \
        /usr/bin/qdistro-tier2-spawn
    install -m 0755 -o root -g root \
        "$QDISTRO_SRC/tier2/podapps-scan.sh" \
        /usr/bin/qdistro-podapps-scan
    # Custom seccomp profiles — deny-by-default allowlists for each
    # tier-2 workload. spawn-tier2.sh looks here as a fallback when
    # SCRIPT_DIR/seccomp/ (dev tree) is absent.
    if [ -d "$QDISTRO_SRC/tier2/seccomp" ]; then
        install -d /usr/lib/qdistro/seccomp
        for _prof in "$QDISTRO_SRC"/tier2/seccomp/*.json; do
            [ -f "$_prof" ] || continue
            install -m 0644 -o root -g root "$_prof" /usr/lib/qdistro/seccomp/
        done
        echo "tier-2 seccomp profiles installed in /usr/lib/qdistro/seccomp/"
    fi
    echo "qdistro-tier2-spawn + qdistro-podapps-scan installed in /usr/bin"
else
    echo "WARN: $QDISTRO_SRC/tier2/spawn-tier2.sh not found —" \
         "PodApps.launch() will fail with 'qdistro-tier2-spawn: not found'." \
         "Pass QDISTRO_SRC=<path> or untar qdistro to /root/qdistro-src/qdistro/."
fi

# 3d. Tier-2 podapps cache directory. qdistro-podapps-scan writes
# /var/lib/qdistro/podapps/<container>/apps.json — by default the dir
# is root-owned 0755 so the admin user can't refresh the cache from
# the live launcher path. Make it group-writable for the admin user's
# primary group (admin on Tumbleweed, users/wheel on others) with the
# group-sticky bit so newly-created subdirs inherit the group.
if ! id admin >/dev/null 2>&1; then
    echo "ERROR: admin user missing — run the bootstrap step that" \
         "creates the user account before this install" >&2
    exit 2
fi
ADMIN_GROUP=$(id -gn admin)
install -d -o root -g "$ADMIN_GROUP" -m 02775 /var/lib/qdistro/podapps
echo "/var/lib/qdistro/podapps perms: $(stat -c '%U:%G %a' /var/lib/qdistro/podapps)"

# 4. User systemd units.
install -d -o admin -g users -m 0755 /home/admin/.config/systemd/user

cat > /home/admin/.config/systemd/user/ydotoold.service <<'EOF'
[Unit]
Description=ydotool synthetic input daemon for qdistro VM tests

[Service]
Type=simple
Environment=YDOTOOL_SOCKET=/run/user/1000/ydotool.sock
ExecCondition=/bin/sh -c 'test -e /sys/module/uinput && test -w /dev/uinput'
ExecStart=/usr/bin/ydotoold --socket-path=/run/user/1000/ydotool.sock --socket-perm=0600
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

# Vendored libweston: qdwin's layer-shell popup parenting needs the
# soft-linked helper symbols that only exist in qdistro's patched
# libweston-16 (see qdwin/doc/decisions/0001-vendored-libweston-packaging.md).
# install-vendored-libweston.sh stages a self-contained tree under
# $QDWIN_LIBWESTON. The system `weston` binary loads the patched core
# via LD_LIBRARY_PATH and ALL backends from the same tree via
# WESTON_MODULE_MAP — core and backends must come from one build (the
# core<->backend ABI is internal to libweston). If the vendored tree is
# absent the unit still starts against the distro libweston, but
# layer-popup grab paths log DEGRADED and Quickshell popups parented to
# layer surfaces will not grab; that is the documented fallback.
QDWIN_LIBWESTON=${QDWIN_LIBWESTON:-/usr/libexec/qdistro/qdwin-libweston}
QDWIN_LIBWESTON_MODDIR="$QDWIN_LIBWESTON/lib64/libweston-16"
QDWIN_MODULE_MAP=""
QDWIN_VENDORED=0
if [ -f "$QDWIN_LIBWESTON_MODDIR/drm-backend.so" ]; then
    # Vendored: load the patched core via LD_LIBRARY_PATH and map every
    # backend to the SAME tree (core<->backend ABI is internal).
    QDWIN_VENDORED=1
    QDWIN_MOD_BASE="$QDWIN_LIBWESTON_MODDIR"
    echo "qdwin session: vendored libweston present — modules mapped to $QDWIN_LIBWESTON_MODDIR"
else
    # Fallback: distro libweston-16, default loader path (no
    # LD_LIBRARY_PATH so the absent vendored dir is not in the search
    # path). Layer-popup grab degrades — documented fallback.
    QDWIN_MOD_BASE="/usr/lib64/libweston-16"
    echo "WARN: vendored libweston not found at $QDWIN_LIBWESTON_MODDIR —" \
         "qdwin session will use distro libweston (layer-popup grab DEGRADED)." \
         "Run install-vendored-libweston.sh to ship the patched tree."
fi
for _mod in drm-backend.so gl-renderer.so color-lcms.so \
            headless-backend.so pipewire-backend.so rdp-backend.so \
            wayland-backend.so x11-backend.so; do
    # NB: xwayland.so is deliberately NOT in this list — it is enabled via
    # `[core] xwayland=true`, not loaded as a backend, and is mapped separately
    # below (its name->path entry still goes through WESTON_MODULE_MAP).
    # In the vendored case only map modules that actually exist (the
    # production build may omit, e.g., vnc); in the distro fallback map
    # the full set (the distro package ships them all).
    if [ "$QDWIN_VENDORED" = 1 ] && [ ! -f "$QDWIN_MOD_BASE/$_mod" ]; then
        continue
    fi
    QDWIN_MODULE_MAP="${QDWIN_MODULE_MAP:+$QDWIN_MODULE_MAP;}$_mod=$QDWIN_MOD_BASE/$_mod"
done

# XWayland: weston's `xwayland.so` is a libweston-16 module (same dir as the
# backends: vendored $QDWIN_LIBWESTON/lib64/libweston-16/ if the production
# build shipped one, else the distro /usr/lib64/libweston-16/xwayland.so from
# the weston package). We map it by name->path through WESTON_MODULE_MAP and
# enable it the SUPPORTED weston-16 way: `[core] xwayland=true`. The old
# `modules=xwayland.so` load is FATAL on weston 14 ("Old Xwayland module
# loading detected"), so we never use it.
#
# GRACEFUL, never fail-closed: if a module is found we map it + flip the
# weston.ini `xwayland=false` placeholder to `xwayland=true` (X11 apps work);
# if none is found we leave xwayland=false and the session starts without
# XWayland exactly as before (X11 app tests stay infra-blocked — status quo,
# NOT a golden-build break). A prior revision fail-closed on the wrong path and
# aborted the whole image build; we never do that.
# weston's xwayland.so is a libweston module in the libweston-16/ dir. Prefer
# the vendored copy if the production profile built one (ABI-matched to the
# patched core); otherwise fall back to the DISTRO module
# (/usr/lib64/libweston-16/xwayland.so from the weston package — same pinned
# 14.0.x, so it loads fine against the vendored core via LD_LIBRARY_PATH). The
# current vendored build skips the xwayland subdir, so the distro module is the
# normal case.
QDWIN_XWAYLAND_SO=""
for _cand in "$QDWIN_LIBWESTON/lib64/libweston-16/xwayland.so" \
             "/usr/lib64/libweston-16/xwayland.so"; do
    if [ -f "$_cand" ]; then QDWIN_XWAYLAND_SO="$_cand"; break; fi
done
if [ -n "$QDWIN_XWAYLAND_SO" ]; then
    # Map the module by name -> absolute path (WESTON_MODULE_MAP is honored by
    # weston's xwayland loader) and enable XWayland the SUPPORTED way:
    # `[core] xwayland=true`. NB: the old `modules=xwayland.so` load is FATAL on
    # weston 14 ("Old Xwayland module loading detected"), so we never use it.
    QDWIN_MODULE_MAP="${QDWIN_MODULE_MAP:+$QDWIN_MODULE_MAP;}xwayland.so=$QDWIN_XWAYLAND_SO"
    # Flip the exact placeholder line written in the heredoc above.
    sed -i 's|^xwayland=false$|xwayland=true|' /home/admin/weston.ini
    # sed -i replaces the inode (now root-owned); weston reads it as admin, so
    # restore ownership for cleanliness/consistency with the rest of the file.
    chown admin:users /home/admin/weston.ini
    echo "qdwin session: XWayland enabled (xwayland=true) — xwayland.so mapped to $QDWIN_XWAYLAND_SO"
else
    echo "WARN: no xwayland.so under vendored or /usr/lib64/libweston-16/ —" \
         "qdwin session starts WITHOUT XWayland (X11 app tests infra-blocked)." \
         "Install the weston package / restage libweston with the xwayland module."
fi

# Only emit the LD_LIBRARY_PATH line in the vendored case — an empty
# LD_LIBRARY_PATH is a (minor) loader smell, and the distro library is on
# the default search path anyway.
if [ "$QDWIN_VENDORED" = 1 ]; then
    QDWIN_LD_LINE="Environment=LD_LIBRARY_PATH=$QDWIN_LIBWESTON/lib64"
else
    QDWIN_LD_LINE="# (distro libweston on default loader path; no LD_LIBRARY_PATH)"
fi

# Compositor unit. Mirrors deploy/qdwin-compositor.service: PartOf= the
# session target (stop/restart propagates from the target). NO [Install]
# section — the unit is pulled in by qdwin-session.target's Requires=, so
# it must NOT be enabled directly under default.target (that would
# auto-start a second compositor racing the target for wayland-1).
# Keeps the VM-tuned XDG_RUNTIME_DIR + dynamic module map / LD path.
cat > /home/admin/.config/systemd/user/qdwin-compositor.service <<EOF
[Unit]
Description=qdwin compositor (libweston + qdwin-shell.so)
PartOf=qdwin-session.target

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-1
Environment=XDG_SESSION_TYPE=wayland
$QDWIN_LD_LINE
Environment=WESTON_MODULE_MAP=$QDWIN_MODULE_MAP
ExecStart=/usr/bin/weston --config=%h/weston.ini --socket=wayland-1
Restart=on-failure
RestartSec=2
EOF

# Shell unit. Mirrors deploy/qdshell.service: After=/Requires= the
# compositor, PartOf= the target, with deploy's start-limit guard. NO
# [Install] — wired into the target via a .wants/ symlink (written below)
# plus the target's Wants=.
cat > /home/admin/.config/systemd/user/qdshell.service <<'EOF'
[Unit]
Description=qdshell desktop (Quickshell QML on top of qdwin)
After=qdwin-compositor.service
Requires=qdwin-compositor.service
PartOf=qdwin-session.target
StartLimitBurst=5
StartLimitIntervalSec=30s

[Service]
Type=simple
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=WAYLAND_DISPLAY=wayland-1
Environment=XDG_SESSION_TYPE=wayland
Environment=QML_DISABLE_DISK_CACHE=1
# Tell qs to look in /usr/share/qdistro/qml for the
# Qdistro.Qdwin QML plugin (libqdistro-qdwin.so installed in step 3b).
# Without this, qdshell's Services/Qdwin/Qdwin.qml cannot resolve
# `import Qdistro.Qdwin 1.0` and the qdwin_shell_v1 binding stays
# unbound.
Environment=QML_IMPORT_PATH=/usr/share/qdistro/qml
ExecStartPre=/bin/sh -c 'i=0; while [ ! -e "$XDG_RUNTIME_DIR/wayland-1" ]; do i=$((i+1)); [ $i -gt 20 ] && exit 1; sleep 0.25; done'
ExecStart=/usr/bin/dbus-run-session -- /usr/bin/qs -p /usr/share/quickshell/qdshell
Restart=on-failure
RestartSec=1
EOF

# Session target. Mirrors deploy/qdwin-session.target: Requires= the
# compositor (hard backbone — target tears down if the compositor dies),
# Wants= the shell (recoverable). [Install] WantedBy=default.target so the
# lingering admin user manager auto-starts the desktop in this headless
# VM. NOTE: qdlocker.service is wired into the target by fresh-vm-bootstrap
# / image/config.sh when present, not here (the locker is a separate repo).
cat > /home/admin/.config/systemd/user/qdwin-session.target <<'EOF'
[Unit]
Description=qdistro desktop session (qdwin compositor + qdshell)
Wants=qdshell.service
Requires=qdwin-compositor.service
After=qdwin-compositor.service

[Install]
WantedBy=default.target
EOF

# Materialize the target -> shell Wants= as an on-disk .wants/ symlink so
# the enabled graph matches deploy/image even before a live `enable` runs.
install -d -o admin -g users -m 0755 \
    /home/admin/.config/systemd/user/qdwin-session.target.wants
ln -sf ../qdshell.service \
    /home/admin/.config/systemd/user/qdwin-session.target.wants/qdshell.service

chown -R admin:users /home/admin/.config/systemd

# 4c. Polkit rule: let `admin` lock its own logind session without
# prompting for an admin password.
#
# org.freedesktop.login1.Session.Lock is gated by
# org.freedesktop.login1.lock-sessions, whose default policy on
# Tumbleweed is auth_admin (full root password). qdistro's locker
# integration (s103-locker-idle.sh Test 4) drives this method to
# simulate HandleLidSwitch=lock — without the rule below the call
# fails with PolicyKit "Not authorized" and the lid-close PASS string
# never fires. 50- prefix so a 10-* rule in /etc/polkit-1/rules.d/
# can still override per-site.
install -d -m 0755 /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-qdistro-locker-idle.rules <<'EOF'
// qdistro: admin may Lock its own logind session without auth.
// Mirrors HandleLidSwitch=lock semantics for headless test VMs.
polkit.addRule(function(action, subject) {
    if (action.id === "org.freedesktop.login1.lock-sessions" &&
        subject.user === "admin") {
        return polkit.Result.YES;
    }
    return undefined;
});
EOF

# 5. Enable (but don't start — caller decides). Enable the SESSION TARGET
# (not the services directly): the target Requires= the compositor and
# Wants= the shell, so enabling + starting the target brings up the whole
# session in the right order. ydotoold is VM-test-only support, enabled
# independently. The greeter image must remove the resulting
# default.target.wants/qdwin-session.target symlink (the greeter starts
# the target explicitly) — image/config.sh handles that.
runuser -l admin -c 'systemctl --user enable qdwin-session.target ydotoold.service' \
    2>&1 || echo "WARN: enable failed (admin user manager not running yet?)"

echo "qdwin session installed (deploy-named units: qdwin-compositor.service + qdshell.service + qdwin-session.target)."
echo "  start now:    runuser -l admin -c 'systemctl --user start qdwin-session.target'"
echo "  start at boot: systemctl --user --machine=admin@ start qdwin-session.target  (after reboot/relogin)"
