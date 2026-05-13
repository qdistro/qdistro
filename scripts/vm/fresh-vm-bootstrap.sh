#!/bin/bash
# One-shot bootstrap for a fresh baseweed-derived VM: pulls the qdwin
# source tree + probe scripts from host:8765, builds qdwin-shell.so +
# qdistro-forward, installs into /usr, and stages /root scripts so
# the host-side bats suite (tests/integration/vm/) can drive the smokes.
#
# Prereqs on the VM:
#   - SELinux permissive (virt-customize before first boot).
#   - zypper install of weston + deps (see tests/integration/vm/README.md).
#   - User 'admin' present (comes from baseweed).
#   - /home/admin/qdwin-rdp/{rdp.crt,rdp.key} already generated via
#     winpr-makecert.
#   - Host HTTP server serving compositor/ at 10.0.2.2:8765.
set -eo pipefail

HOST=http://10.0.2.2:8765
SRC=/root/qdwin-src

echo "[bootstrap] syncing source tree..."
mkdir -p "$SRC"/{qdwin,qdistro-forward,qdistro-nested-pixelfeed,qdistro-cursor-sprites,qdistro-secctx-exec,qdshell/protocol,qdshell/modules,test-client}
wget -q -O "$SRC/meson.build"                           "$HOST/meson.build"
wget -q -O "$SRC/qdwin/qdwin.c"                         "$HOST/qdwin/qdwin.c"
wget -q -O "$SRC/qdwin/qdwin-nested-client.c"           "$HOST/qdwin/qdwin-nested-client.c"
wget -q -O "$SRC/qdwin/qdwin-nested-client.h"           "$HOST/qdwin/qdwin-nested-client.h"
wget -q -O "$SRC/qdwin/qdwin-shell-v1.xml"              "$HOST/qdwin/qdwin-shell-v1.xml"
wget -q -O "$SRC/qdwin/wlr-layer-shell-unstable-v1.xml" "$HOST/qdwin/wlr-layer-shell-unstable-v1.xml"
wget -q -O "$SRC/qdistro-forward/qdistro-forward.c"     "$HOST/qdistro-forward/qdistro-forward.c"
wget -q -O "$SRC/qdistro-forward/qdistro-forward.py"    "$HOST/qdistro-forward/qdistro-forward.py"
wget -q -O "$SRC/qdistro-nested-pixelfeed/qdistro-nested-pixelfeed.c" \
                                                       "$HOST/qdistro-nested-pixelfeed/qdistro-nested-pixelfeed.c"
wget -q -O "$SRC/qdistro-cursor-sprites/qdistro-cursor-sprites.c" \
                                                       "$HOST/qdistro-cursor-sprites/qdistro-cursor-sprites.c"
wget -q -O "$SRC/qdistro-cursor-sprites/qdistro-cursor-sprites.service" \
                                                       "$HOST/qdistro-cursor-sprites/qdistro-cursor-sprites.service"
wget -q -O "$SRC/qdistro-secctx-exec/qdistro-secctx-exec.c" \
                                                       "$HOST/qdistro-secctx-exec/qdistro-secctx-exec.c"
wget -q -O "$SRC/qdshell/qdshell.py"                "$HOST/qdshell/qdshell.py"
wget -q -O "$SRC/qdshell/gen_protocol.sh"           "$HOST/qdshell/gen_protocol.sh"
wget -q -O "$SRC/qdshell/README.md"                 "$HOST/qdshell/README.md"
# §6.6 modules package — panel/tray/notifications/launcher/switcher/
# locker/broker/chrome carve-outs. `__init__.py` binds the module
# imports so `from modules.panel import install_panel` works in
# qdshell.py.
for m in __init__.py panel.py tray.py notifications.py launcher.py \
         switcher.py locker.py broker.py chrome.py main.py; do
    wget -q -O "$SRC/qdshell/modules/$m" "$HOST/qdshell/modules/$m"
done
chmod +x "$SRC/qdshell/gen_protocol.sh"
wget -q -O "$SRC/test-client/qdwin-probe.c"             "$HOST/test-client/qdwin-probe.c"
wget -q -O "$SRC/test-client/qdistro-test-window.c"     "$HOST/test-client/qdistro-test-window.c"
wget -q -O "$SRC/test-client/qdistro-test-clipboard-source.c" "$HOST/test-client/qdistro-test-clipboard-source.c"
wget -q -O "$SRC/test-client/qdistro-test-clipboard-sink.c"   "$HOST/test-client/qdistro-test-clipboard-sink.c"

echo "[bootstrap] staging probe scripts..."
wget -q -O /root/pw-setup.sh               "$HOST/spike-6.5/pw-setup.sh"
wget -q -O /root/s3c-sync-and-build.sh     "$HOST/spike-6.5/s3c-sync-and-build.sh"
wget -q -O /root/s3c-e2e.sh                "$HOST/spike-6.5/s3c-e2e.sh"
wget -q -O /root/s5a-e2e.sh                "$HOST/spike-6.5/s5a-e2e.sh"
wget -q -O /root/s5c-e2e.sh                "$HOST/spike-6.5/s5c-e2e.sh"
wget -q -O /root/s3c-idle-gate.sh          "$HOST/spike-6.5/s3c-idle-gate-probe.sh"
wget -q -O /root/s3c-sdl-dummy.sh          "$HOST/spike-6.5/s3c-sdl-dummy-probe.sh"
wget -q -O /root/s4-broker-gate.sh         "$HOST/spike-6.5/s4-broker-gate-probe.sh"
wget -q -O /root/s4-revoke-teardown.sh     "$HOST/spike-6.5/s4-revoke-teardown-probe.sh"
wget -q -O /root/s6-v2-events.sh           "$HOST/spike-6.5/s6-v2-events-probe.sh"
wget -q -O /root/s7-xdg-activation.sh      "$HOST/spike-6.5/s7-xdg-activation.sh"
wget -q -O /root/s7-xdg-activation-probe.py "$HOST/spike-6.5/s7-xdg-activation-probe.py"
wget -q -O /root/s8-protocol-globals.sh    "$HOST/spike-6.5/s8-protocol-globals.sh"
wget -q -O /root/s8-protocol-globals-probe.py "$HOST/spike-6.5/s8-protocol-globals-probe.py"
wget -q -O /root/s9-primary-selection.sh    "$HOST/spike-6.5/s9-primary-selection.sh"
wget -q -O /root/s9-primary-selection-probe.py "$HOST/spike-6.5/s9-primary-selection-probe.py"
wget -q -O /root/s9-primary-selection-c.sh   "$HOST/spike-6.5/s9-primary-selection-c.sh"
wget -q -O /root/s9-primary-selection.c      "$HOST/spike-6.5/s9-primary-selection.c"
wget -q -O /root/s10-panel.sh                "$HOST/spike-6.5/s10-panel.sh"
wget -q -O /root/s11-notifications.sh        "$HOST/spike-6.5/s11-notifications.sh"
wget -q -O /root/s12-launcher.sh             "$HOST/spike-6.5/s12-launcher.sh"
wget -q -O /root/s13-locker.sh               "$HOST/spike-6.5/s13-locker.sh"
wget -q -O /root/s14-locker-auth.sh          "$HOST/spike-6.5/s14-locker-auth.sh"
wget -q -O /root/s15-nested-hosting.sh       "$HOST/spike-6.5/s15-nested-hosting.sh"
wget -q -O /root/s16-greetd-cutover.sh       "$HOST/spike-6.5/s16-greetd-cutover.sh"
wget -q -O /root/s17-notify-relay.sh         "$HOST/spike-6.5/s17-notify-relay.sh"
wget -q -O /root/s18-approval-app.sh         "$HOST/spike-6.5/s18-approval-app.sh"
wget -q -O /root/s19-approval-app-tray.sh    "$HOST/spike-6.5/s19-approval-app-tray.sh"
wget -q -O /root/s20-cursor-sprite-install.sh "$HOST/spike-6.5/s20-cursor-sprite-install.sh"
wget -q -O /root/s21-nested-v1-bind.sh       "$HOST/spike-6.5/s21-nested-v1-bind.sh"
wget -q -O /root/s21-nested-probe.py         "$HOST/spike-6.5/s21-nested-probe.py"
wget -q -O /root/s22-nested-pw-probe.sh      "$HOST/spike-6.5/s22-nested-pw-probe.sh"
wget -q -O /root/s23-nested-publish.sh       "$HOST/spike-6.5/s23-nested-publish.sh"
wget -q -O /root/s24-nested-broker-gate.sh   "$HOST/spike-6.5/s24-nested-broker-gate.sh"
wget -q -O /root/s25-nested-input-decode.sh  "$HOST/spike-6.5/s25-nested-input-decode.sh"
wget -q -O /root/s26-nested-pixel-bind.sh    "$HOST/spike-6.5/s26-nested-pixel-bind.sh"
wget -q -O /root/s27-nested-pw-pixels.sh     "$HOST/spike-6.5/s27-nested-pw-pixels.sh"
wget -q -O /root/s28-nested-keyboard-grab.sh "$HOST/spike-6.5/s28-nested-keyboard-grab.sh"
wget -q -O /root/s29-cursor-sprites.sh       "$HOST/spike-6.5/s29-cursor-sprites.sh"
wget -q -O /root/s30-approval-app-phase2.sh  "$HOST/spike-6.5/s30-approval-app-phase2.sh"
wget -q -O /root/s31-pixelfeed-dmabuf.sh     "$HOST/spike-6.5/s31-pixelfeed-dmabuf.sh"
wget -q -O /root/s32-tier2-podman.sh         "$HOST/spike-6.5/s32-tier2-podman.sh"
wget -q -O /root/s33-tier2-input.sh          "$HOST/spike-6.5/s33-tier2-input.sh"
wget -q -O /root/s34-tier2-lifecycle.sh      "$HOST/spike-6.5/s34-tier2-lifecycle.sh"
wget -q -O /root/s35-tier3-waypipe.sh        "$HOST/spike-6.5/s35-tier3-waypipe.sh"
wget -q -O /root/s36-tier3-app.sh            "$HOST/spike-6.5/s36-tier3-app.sh"
wget -q -O /root/s37-tier3-lifecycle.sh      "$HOST/spike-6.5/s37-tier3-lifecycle.sh"
wget -q -O /root/s38-tier3-chrome.sh         "$HOST/spike-6.5/s38-tier3-chrome.sh"
wget -q -O /root/s39-clipboard-gate.sh       "$HOST/spike-6.5/s39-clipboard-gate.sh"
wget -q -O /root/s40-secctx.sh               "$HOST/spike-6.5/s40-secctx.sh"
wget -q -O /root/s41-secctx-toplevel-event.sh "$HOST/spike-6.5/s41-secctx-toplevel-event.sh"
wget -q -O /root/s42-tier4-spawn.sh           "$HOST/spike-6.5/s42-tier4-spawn.sh"
wget -q -O /root/s43-tier5-loopback.sh         "$HOST/spike-6.5/s43-tier5-loopback.sh"
wget -q -O /root/s44-tier4-secctx-exec.sh      "$HOST/spike-6.5/s44-tier4-secctx-exec.sh"
wget -q -O /root/s45-tier5-vm.sh               "$HOST/spike-6.5/s45-tier5-vm.sh"
wget -q -O /root/s46-tier4-clipboard-gate.sh   "$HOST/spike-6.5/s46-tier4-clipboard-gate.sh"
wget -q -O /root/s47-tier5-audio.sh            "$HOST/spike-6.5/s47-tier5-audio.sh"
wget -q -O /root/s48-focus-aware-clear.sh      "$HOST/spike-6.5/s48-focus-aware-clear.sh"
wget -q -O /root/s49-tier4-spice-clipboard.sh  "$HOST/spike-6.5/s49-tier4-spice-clipboard.sh"
wget -q -O /root/s50-tier1-skeleton.sh         "$HOST/spike-6.5/s50-tier1-skeleton.sh"
wget -q -O /root/s51-tier1-e2e.sh              "$HOST/spike-6.5/s51-tier1-e2e.sh"
wget -q -O /root/s52-tier1-audisp.sh           "$HOST/spike-6.5/s52-tier1-audisp.sh"
wget -q -O /root/s53-data-offer-receive-v15.sh "$HOST/spike-6.5/s53-data-offer-receive-v15.sh"
wget -q -O /root/s54-tier4-spice-clipboard-live.sh \
    "$HOST/spike-6.5/s54-tier4-spice-clipboard-live.sh"
wget -q -O /root/s55-tier1-enforcing.sh        "$HOST/spike-6.5/s55-tier1-enforcing.sh"
wget -q -O /root/s56-broker-enforcing.sh       "$HOST/spike-6.5/s56-broker-enforcing.sh"
wget -q -O /root/s57-qsu-argv-scopes.sh        "$HOST/spike-6.5/s57-qsu-argv-scopes.sh"
wget -q -O /root/s58-qsu-real-flow.sh          "$HOST/spike-6.5/s58-qsu-real-flow.sh"
wget -q -O /root/s60-pwd-e2e.sh                "$HOST/spike-6.5/s60-pwd-e2e.sh"
wget -q -O /root/s61-pwd-tpm-e2e.sh            "$HOST/spike-6.5/s61-pwd-tpm-e2e.sh"
wget -q -O /root/s62-pwd-portal-autounlock-e2e.sh \
                                                "$HOST/spike-6.5/s62-pwd-portal-autounlock-e2e.sh"
wget -q -O /root/s63-print-vm-helpers-probe.sh  "$HOST/spike-6.5/s63-print-vm-helpers-probe.sh"
wget -q -O /root/s64-print-allowlist-caps-probe.sh \
                                                "$HOST/spike-6.5/s64-print-allowlist-caps-probe.sh"
wget -q -O /root/s65-pwd-fprint-probe.sh        "$HOST/spike-6.5/s65-pwd-fprint-probe.sh"
wget -q -O /root/s66-browser-bridge-probe.sh    "$HOST/spike-6.5/s66-browser-bridge-probe.sh"
wget -q -O /root/s67-recall-probe.sh            "$HOST/spike-6.5/s67-recall-probe.sh"
wget -q -O /root/s68-snapshots-probe.sh         "$HOST/spike-6.5/s68-snapshots-probe.sh"
wget -q -O /root/s69-phone-probe.sh             "$HOST/spike-6.5/s69-phone-probe.sh"
chmod +x /root/s60-pwd-e2e.sh /root/s61-pwd-tpm-e2e.sh \
         /root/s62-pwd-portal-autounlock-e2e.sh \
         /root/s63-print-vm-helpers-probe.sh \
         /root/s64-print-allowlist-caps-probe.sh \
         /root/s65-pwd-fprint-probe.sh \
         /root/s66-browser-bridge-probe.sh \
         /root/s67-recall-probe.sh \
         /root/s68-snapshots-probe.sh \
         /root/s69-phone-probe.sh

# §Phase-1: RDP TLS cert/key for weston-rdp. baseweed.qcow2 USED to
# carry pre-generated certs but fresh clones from a stripped baseweed
# don't always have them. Generate idempotently here so the bats
# suite isn't a precondition-archaeology exercise.
if [ ! -f /home/admin/qdwin-rdp/rdp.crt ] || [ ! -f /home/admin/qdwin-rdp/rdp.key ]; then
    echo "[bootstrap] generating RDP TLS cert/key (winpr-makecert)..."
    wget -q -O /root/setup-rdp-cert.sh "$HOST/spike-6.5/setup-rdp-cert.sh"
    bash /root/setup-rdp-cert.sh >/dev/null 2>&1 || \
        echo "[bootstrap] WARN: setup-rdp-cert failed; phase6.5 bats will fail"
fi

# §Phase-7 tier-2: pull the container build context + build the image.
# Image build is idempotent; if it's already present, podman reuses
# layers. Skip the build when podman isn't installed.
mkdir -p /root/tier2
for f in Containerfile tier2-entry.sh weston-nested.ini \
         build-tier2-container.sh run-tier2-container.sh; do
    wget -q -O /root/tier2/$f "$HOST/spike-6.5/tier2/$f"
done
chmod +x /root/tier2/*.sh

# §Phase-7 broker policy module (task 058 / spec/30 hygiene Phase 1).
# Declares qdistro_broker_t / qdistro_broker_exec_t and exports
# qdistro_broker_dbus_chat() so other modules (today: tier1) can stop
# inlining `allow ...:dbus send_msg` against system_dbusd_t. Must
# compile-load BEFORE qdistro_tier1 since tier1 calls the interface
# and gen_requires the broker types.
mkdir -p /root/broker-policy
for f in Makefile install-policy.sh qdistro_broker.te \
         qdistro_broker.if qdistro_broker.fc; do
    wget -q -O /root/broker-policy/$f "$HOST/spike-6.5/broker-policy/$f"
done
chmod +x /root/broker-policy/install-policy.sh

# §Phase-8.4 spec/13 pwd policy module. Mirrors broker-policy
# layout. install-pwd-for-vm.sh runs install-policy.sh after
# staging the daemon — but stage the .te/.if/.fc here so the
# install path can find them under /root/pwd-policy/.
mkdir -p /root/pwd-policy
for f in Makefile install-policy.sh qdistro_pwd.te \
         qdistro_pwd.if qdistro_pwd.fc; do
    wget -q -O /root/pwd-policy/$f "$HOST/spike-6.5/pwd-policy/$f"
done
chmod +x /root/pwd-policy/install-policy.sh

# §Phase-7 tier-1: SELinux sandbox (spec/30). The policy module
# `qdistro_tier1.pp` compiles + loads via install-policy.sh; the
# C wrapper `qdistro-tier1-exec` is built from daemons/tier1-exec/.
# tier1-exec/ via meson and shipped to /usr/bin; the spawn-tier1.sh
# bash wrapper goes to /usr/local/bin/qdistro-tier1-spawn.
mkdir -p /root/tier1
for f in README.md spike-checklist.md spawn-tier1.sh install-policy.sh \
         Makefile qdistro_tier1.te qdistro_tier1.if qdistro_tier1.fc; do
    wget -q -O /root/tier1/$f "$HOST/spike-6.5/tier1/$f"
done
chmod +x /root/tier1/spawn-tier1.sh /root/tier1/install-policy.sh
install -m 0755 /root/tier1/spawn-tier1.sh /usr/local/bin/qdistro-tier1-spawn
# Pull the C wrapper source into the meson build tree.
mkdir -p "$SRC/qdistro-tier1-exec"
wget -q -O "$SRC/qdistro-tier1-exec/qdistro-tier1-exec.c" \
    "$HOST/qdistro-tier1-exec/qdistro-tier1-exec.c"

# §Phase-7 tier-3: cross-uid waypipe bridge wrapper.
mkdir -p /root/tier3
wget -q -O /root/tier3/spawn-tier3.sh "$HOST/spike-6.5/tier3/spawn-tier3.sh"
chmod +x /root/tier3/spawn-tier3.sh
install -m 0755 /root/tier3/spawn-tier3.sh /usr/local/bin/qdistro-tier3-spawn

# §Phase-7 tier-4: libvirt + virt-viewer wrapper. Linux-only per spec/00.
mkdir -p /root/tier4
for f in spawn-tier4.sh qdistro-tier4-cleanup.sh domain-template.xml \
         build-guest-image.sh README.md; do
    wget -q -O /root/tier4/$f "$HOST/spike-6.5/tier4/$f"
done
chmod +x /root/tier4/spawn-tier4.sh /root/tier4/qdistro-tier4-cleanup.sh \
         /root/tier4/build-guest-image.sh
install -m 0755 /root/tier4/spawn-tier4.sh           /usr/local/bin/qdistro-tier4-spawn
install -m 0755 /root/tier4/qdistro-tier4-cleanup.sh /usr/local/bin/qdistro-tier4-cleanup
install -m 0755 /root/tier4/build-guest-image.sh     /usr/local/bin/qdistro-tier4-build-guest-image
install -d                                            /usr/share/qdistro/tier4
install -m 0644 /root/tier4/domain-template.xml      /usr/share/qdistro/tier4/domain-template.xml
# Make the installed wrapper find the template at the standard path
# instead of the dev path under /root.
ln -sf /usr/share/qdistro/tier4/domain-template.xml /root/tier4/domain-template.xml.installed 2>/dev/null || true

# §Phase-7 tier-5-Linux: waypipe-over-vsock wrapper. Linux-only per spec/00.
mkdir -p /root/tier5
for f in spawn-tier5.sh qdistro-tier5-cleanup.sh build-guest-image.sh domain-template.xml README.md; do
    wget -q -O /root/tier5/$f "$HOST/spike-6.5/tier5/$f"
done
chmod +x /root/tier5/spawn-tier5.sh /root/tier5/qdistro-tier5-cleanup.sh /root/tier5/build-guest-image.sh
install -m 0755 /root/tier5/spawn-tier5.sh           /usr/local/bin/qdistro-tier5-spawn
install -m 0755 /root/tier5/qdistro-tier5-cleanup.sh /usr/local/bin/qdistro-tier5-cleanup
install -m 0755 /root/tier5/build-guest-image.sh     /usr/local/bin/qdistro-tier5-build-guest-image
install -d                                            /usr/share/qdistro/tier5
install -m 0644 /root/tier5/domain-template.xml      /usr/share/qdistro/tier5/domain-template.xml
# Optional: build the tier-5 per-app guest base disk so phase7-tier5-vm
# and phase7-tier5-audio graduate from SKIP → hard PASS. Default is OFF
# to keep the dev cycle fast (the build wgets ~400MB of openSUSE
# Minimal-VM cloud qcow2 + virt-customizes ~30s). Enable on validation
# runs by setting QDISTRO_BUILD_TIER5_BASE=1 on the bootstrap caller.
# Idempotent — the build script skips if the disk already exists.
if [ "${QDISTRO_BUILD_TIER5_BASE:-0}" = "1" ]; then
    echo "[bootstrap] building tier-5 base disk (QDISTRO_BUILD_TIER5_BASE=1)..."
    # libguestfs + qemu-tools provide virt-customize and qemu-img;
    # neither is in baseweed by default. Install on demand.
    for pkg in libguestfs guestfs-tools qemu-tools; do
        rpm -q "$pkg" >/dev/null 2>&1 || \
            zypper -n install "$pkg" >/dev/null 2>&1 || \
            echo "[bootstrap] WARN: $pkg install failed (tier-5 base build will fail)"
    done
    # The build wgets from download.opensuse.org. Override with a
    # host-cached mirror if QDISTRO_TIER5_BASE_MIRROR is set (faster
    # + works offline once primed).
    BUILD_ARGS=()
    if [ -n "${QDISTRO_TIER5_BASE_MIRROR:-}" ]; then
        BUILD_ARGS+=(--mirror "$QDISTRO_TIER5_BASE_MIRROR")
    fi
    if /usr/local/bin/qdistro-tier5-build-guest-image "${BUILD_ARGS[@]}"; then
        echo "[bootstrap] tier-5 base disk ready"
    else
        echo "[bootstrap] tier-5 base build FAILED — phase7-tier5-* will SKIP" >&2
    fi
fi
# vsock_loopback module is needed for the smoke test; auto-loaded by
# the wrapper at runtime (modprobe vsock_loopback) but lazy-loading is
# fine. /etc/modules-load.d entry is optional polish for production.
# tier-2 image build needs the host-installed qdwin-shell.so, so it has
# to run AFTER the meson build below. See the second-pass invocation
# down near the broker install.
wget -q -O "$SRC/qdwin/qdwin-nested-v1.xml"  "$HOST/qdwin/qdwin-nested-v1.xml"
wget -q -O "$SRC/qdshell/qdistro-admin-approval-app.py" \
    "$HOST/qdshell/qdistro-admin-approval-app.py"
# §6.6 S7: deploy tree is also needed on-VM so s16/s17 can invoke
# bootstrap-qdwin-in-vm.sh against the installed sources.
mkdir -p "$SRC/deploy"
wget -q -O "$SRC/deploy/bootstrap-qdwin-in-vm.sh"  "$HOST/deploy/bootstrap-qdwin-in-vm.sh"
wget -q -O "$SRC/deploy/greetd-qdwin.service"      "$HOST/deploy/greetd-qdwin.service"
wget -q -O "$SRC/deploy/greetd-config-qdwin.toml"  "$HOST/deploy/greetd-config-qdwin.toml"
wget -q -O "$SRC/deploy/qdistro-start-qdwin.sh"    "$HOST/deploy/qdistro-start-qdwin.sh"
wget -q -O "$SRC/deploy/com.qdistro.Notifications1.conf" \
    "$HOST/deploy/com.qdistro.Notifications1.conf"
wget -q -O "$SRC/deploy/qdistro-notify-send.py"    "$HOST/deploy/qdistro-notify-send.py"
chmod +x "$SRC/deploy/bootstrap-qdwin-in-vm.sh" \
         "$SRC/deploy/qdistro-start-qdwin.sh" \
         "$SRC/deploy/qdistro-notify-send.py"
wget -q -O /root/sync-broker.sh            "$HOST/spike-6.5/sync-broker.sh"
wget -q -O /root/install-broker-for-qdwin.sh \
                                            "$HOST/spike-6.5/install-broker-for-qdwin.sh"
wget -q -O /root/install-pwd-for-vm.sh     "$HOST/spike-6.5/install-pwd-for-vm.sh"
chmod +x /root/install-pwd-for-vm.sh
mkdir -p /root/pwd-src
for f in qdistro_pwd_daemon.py qdistro_pwd_vault.py qdistro_pwd_identity.py \
         qdistro_pwd_audit.py qdistro_pwd_tpm.py qdistro_pwd_polkit.py \
         qdistro_pwd_portal.py qdistro_pwd_pinstash.py \
         qdistro_pwd_fprint.py \
         qdistro-pwd-admin.py qdistro-pwd-get.py \
         com.qdistro.Pwd1.conf qdistro-pwd.service \
         qdistro-pwd-portal.service qdistro-portal-keys-unlock.service \
         org.qdistro.PortalSecret.portal \
         qdistro-portals.conf \
         com.qdistro.pwd.policy qdistro-pwd.rules \
         qdistro-pwd-fprint.rules; do
    wget -q -O "/root/pwd-src/$f" "$HOST/spike-6.5/pwd/$f"
done

# §spec/13 admin polkit AuthenticationAgent — PAM / fprintd / broker
# dispatch. Per-user session daemon; install-polkit-agent-for-vm.sh
# enables it under admin's --user systemd, so it auto-registers with
# polkitd at session start.
wget -q -O /root/install-polkit-agent-for-vm.sh \
    "$HOST/spike-6.5/install-polkit-agent-for-vm.sh"
chmod +x /root/install-polkit-agent-for-vm.sh
mkdir -p /root/polkit-src
for f in qdistro_polkit_agent.py qdistro-polkit-prompt.py \
         qdistro-polkit-agent.service polkit-agent.conf; do
    wget -q -O "/root/polkit-src/$f" "$HOST/spike-6.5/polkit/$f"
done

# §Phase-9 spec/20 print proxy (host transport stub for CUPS-in-VM).
wget -q -O /root/install-print-proxy-for-vm.sh \
                                            "$HOST/spike-6.5/install-print-proxy-for-vm.sh"
chmod +x /root/install-print-proxy-for-vm.sh

# §spec/14 Phase-8 MVP browser-bridge native-messaging host.
wget -q -O /root/install-browser-bridge-for-vm.sh \
    "$HOST/spike-6.5/install-browser-bridge-for-vm.sh"
chmod +x /root/install-browser-bridge-for-vm.sh

# §spec/17 §step 0 MVP recall ingest engine + reaper + CLI + SDK.
wget -q -O /root/install-recall-for-vm.sh \
    "$HOST/spike-6.5/install-recall-for-vm.sh"
chmod +x /root/install-recall-for-vm.sh
mkdir -p /root/recall-src/qdistro_app
for f in qdistro_recall_ingest.py qdistro_recall_daemon.py \
         qdistro-recall@.service qdistro-recall@.timer; do
    wget -q -O "/root/recall-src/$f" "$HOST/spike-6.5/recall/$f"
done
wget -q -O "/root/recall-src/qdistro_recall_cli.py" \
    "$HOST/spike-6.5/cli/qdistro_recall_cli.py"
wget -q -O "/root/recall-src/qdistro_app/recall.py" \
    "$HOST/spike-6.5/sdk/qdistro_app/recall.py"
wget -q -O "/root/recall-src/qdistro_app/__init__.py" \
    "$HOST/spike-6.5/sdk/qdistro_app/__init__.py"

# §spec/19 Phase-8 MVP — snapshots engine + CLI + qdistro-backup units.
wget -q -O /root/install-snapshots-for-vm.sh \
    "$HOST/spike-6.5/install-snapshots-for-vm.sh"
chmod +x /root/install-snapshots-for-vm.sh
mkdir -p /root/snapshots-src
for f in qdistro_snapshots.py qdistro_snap_export_cli.py \
         qdistro-backup.service qdistro-backup.timer; do
    wget -q -O "/root/snapshots-src/$f" "$HOST/spike-6.5/snapshots/$f"
done

# §spec/18 Phase-8 MVP — qdistro-phone daemon + CLI.
wget -q -O /root/install-phone-for-vm.sh \
    "$HOST/spike-6.5/install-phone-for-vm.sh"
chmod +x /root/install-phone-for-vm.sh
mkdir -p /root/phone-src
for f in qdistro_phone.py qdistro_phone_daemon.py \
         qdistro_phone_cli.py qdistro-phone.service; do
    wget -q -O "/root/phone-src/$f" "$HOST/spike-6.5/phone/$f"
done

mkdir -p /root/browser-bridge-src/extension/icons
for f in qdistro_browser_bridge.py qdistro_browser_install.py; do
    wget -q -O "/root/browser-bridge-src/$f" \
        "$HOST/spike-6.5/browser_bridge/$f"
done
for f in manifest.firefox.json manifest.chromium.json popup.html \
         popup.js background.js README.md build-extension.sh; do
    wget -q -O "/root/browser-bridge-src/extension/$f" \
        "$HOST/spike-6.5/browser_bridge/extension/$f"
done
wget -q -O "/root/browser-bridge-src/extension/icons/icon-48.png" \
    "$HOST/spike-6.5/browser_bridge/extension/icons/icon-48.png"
chmod +x /root/browser-bridge-src/extension/build-extension.sh

mkdir -p /root/print-src
for f in qdistro_print_proxy.py qdistro_print_audit.py \
         qdistro_print_browse.py \
         qdistro-print-proxy.service spawn-print-vm.sh \
         com.qdistro.print.policy; do
    wget -q -O "/root/print-src/$f" "$HOST/spike-6.5/print/$f"
done
# spec/20 Phase-9 §step 2 — qdistro-print VM scaffolding (image
# builder, libvirt domain template, USB hot-plug helpers). Fetched
# alongside the proxy so install-print-proxy-for-vm.sh stages
# everything in one place.
for f in install-print-vm.sh build-print-image.sh \
         qdistro-print-attach-usb.sh qdistro-print-detach-usb.sh \
         qdistro-print-allowlist qdistro-print-jobs \
         domain-template.xml; do
    wget -q -O "/root/print-src/$f" "$HOST/spike-6.5/print-vm/$f"
done
chmod +x /root/print-src/spawn-print-vm.sh \
         /root/print-src/install-print-vm.sh \
         /root/print-src/build-print-image.sh \
         /root/print-src/qdistro-print-attach-usb.sh \
         /root/print-src/qdistro-print-detach-usb.sh \
         /root/print-src/qdistro-print-allowlist \
         /root/print-src/qdistro-print-jobs 2>/dev/null || true
wget -q -O /root/install-qsu-for-vm.sh      "$HOST/spike-6.5/install-qsu-for-vm.sh"
chmod +x /root/*.sh

# Deploy probes into admin's home (s5a/s5c expect them there).
install -d -o admin -g admin /home/admin/spike-6.5
wget -q -O /home/admin/spike-6.5/s5a-claim-probe.py  "$HOST/spike-6.5/s5a-claim-probe.py"
wget -q -O /home/admin/spike-6.5/s5c-inject-probe.py "$HOST/spike-6.5/s5c-inject-probe.py"
chown -R admin:admin /home/admin/spike-6.5
chmod +x /home/admin/spike-6.5/*.py

echo "[bootstrap] first-time meson setup..."
cd "$SRC"
meson setup build 2>&1 | tail -5
ninja_targets=(qdwin-shell.so qdistro-forward qdistro-nested-pixelfeed
               qdistro-cursor-sprites qdistro-secctx-exec
               qdistro-test-window qdistro-test-clipboard-source
               qdistro-test-clipboard-sink)
# qdistro-tier1-exec is opt-in on libselinux availability; meson
# only declares the executable() when libselinux is found, so listing
# it unconditionally would fail the build on hosts without it.
if [ -e build/qdistro-tier1-exec.p ] || [ -d build/qdistro-tier1-exec.p ] || \
   pkg-config --exists libselinux 2>/dev/null; then
    ninja_targets+=(qdistro-tier1-exec)
fi
ninja -C build "${ninja_targets[@]}" 2>&1 | tail -5
install -m 0755 build/qdwin-shell.so   /usr/lib64/weston/qdwin-shell.so
install -m 0755 build/qdistro-forward  /usr/bin/qdistro-forward
install -m 0755 build/qdistro-nested-pixelfeed /usr/bin/qdistro-nested-pixelfeed
install -m 0755 build/qdistro-cursor-sprites   /usr/bin/qdistro-cursor-sprites
# Drop the cursor-sprites user systemd unit so the helper auto-attaches
# to qdwin whenever noctalia-shell.service starts under admin's --user
# manager. Idempotent install + enable; if admin isn't present yet the
# enable is skipped non-fatally (the §6.6 admin-provisioning step earlier
# in this script ensures admin exists before we get here on baseweed
# clones, but newly-created visual-only VMs may not have admin yet).
if id admin >/dev/null 2>&1; then
    install -d -o admin -g admin /home/admin/.config/systemd/user
    install -m 0644 -o admin -g admin \
        "$SRC/qdistro-cursor-sprites/qdistro-cursor-sprites.service" \
        /home/admin/.config/systemd/user/qdistro-cursor-sprites.service
    runuser -l admin -c \
        'systemctl --user daemon-reload && systemctl --user enable qdistro-cursor-sprites.service' \
        2>/dev/null || \
        echo "[bootstrap] WARN: cursor-sprites enable failed (non-fatal)"
fi
install -m 0755 build/qdistro-secctx-exec      /usr/bin/qdistro-secctx-exec
install -m 0755 build/qdistro-test-window         /usr/bin/qdistro-test-window
install -m 0755 build/qdistro-test-clipboard-source /usr/bin/qdistro-test-clipboard-source
install -m 0755 build/qdistro-test-clipboard-sink   /usr/bin/qdistro-test-clipboard-sink
if [ -x build/qdistro-tier1-exec ]; then
    install -d                                          /usr/libexec
    install -m 0755 build/qdistro-tier1-exec      /usr/libexec/qdistro-tier1-exec
    # PATH-friendly symlink so spawn-tier1.sh's `qdistro-tier1-exec`
    # bare invocation resolves. The labelled path stays /usr/libexec
    # to match qdistro_tier1.fc's gen_context regex.
    ln -sf /usr/libexec/qdistro-tier1-exec /usr/local/bin/qdistro-tier1-exec
    if command -v restorecon >/dev/null 2>&1; then
        restorecon /usr/libexec/qdistro-tier1-exec 2>/dev/null || true
    fi
fi
# §Phase-8.4 spec/13 pwd policy module (must load BEFORE broker —
# broker 0.4.0+ gen_requires qdistro_pwd_audit_t for shared
# /var/lib/qdistro/audit/ manage perms). install-policy.sh is
# idempotent; warnings are non-fatal so a missing selinux-policy-devel
# doesn't wedge bootstrap.
if command -v semodule >/dev/null 2>&1 && \
   [ -f /root/pwd-policy/Makefile ]; then
    bash /root/pwd-policy/install-policy.sh 2>&1 | tail -3 || \
        echo "[bootstrap] WARN: pwd SELinux policy install failed"
fi
# §Phase-7 broker policy module (must load BEFORE tier1 — tier1's
# qdistro_broker_dbus_chat() call gen_requires types from
# qdistro_broker.te; AFTER pwd because broker 0.4.0 references
# qdistro_pwd_audit_t). install-policy.sh is idempotent; warnings
# are non-fatal so a missing selinux-policy-devel doesn't wedge
# bootstrap.
if command -v semodule >/dev/null 2>&1 && \
   [ -f /root/broker-policy/Makefile ]; then
    bash /root/broker-policy/install-policy.sh 2>&1 | tail -3 || \
        echo "[bootstrap] WARN: broker SELinux policy install failed"
fi
# §Phase-7 tier-1 policy module compile + load. install-policy.sh
# is idempotent; running it on every bootstrap keeps the policy in
# sync with /root/tier1/qdistro_tier1.te. A failure here is non-
# fatal: the spawn wrapper warns when the module isn't loaded but
# still exec's the inner command (admin can re-run install-policy.sh
# manually if needed).
if command -v semodule >/dev/null 2>&1 && [ -f /root/tier1/Makefile ]; then
    bash /root/tier1/install-policy.sh 2>&1 | tail -3 || \
        echo "[bootstrap] WARN: tier-1 SELinux policy install failed"
fi
echo "[bootstrap] installed:"
ls -la /usr/lib64/weston/qdwin-shell.so /usr/bin/qdistro-forward

echo "[bootstrap] starting pipewire..."
bash /root/pw-setup.sh
sleep 1
runuser -u admin -- bash -c "XDG_RUNTIME_DIR=/run/user/1000 pw-cli info | head -5" || echo "(pipewire may need a re-run)"

echo "[bootstrap] installing python-pam + fprintd for §6.6 S5 auth..."
# §6.6 S5 full: locker needs python-pam for password auth and
# fprintd for fingerprint auth. Both are optional at runtime (locker
# degrades gracefully), but §6.6 S5 bats expects them present.
for pkg in python313-python-pam fprintd; do
    rpm -q "$pkg" >/dev/null 2>&1 || \
        zypper -n install "$pkg" >/dev/null 2>&1 || \
        echo "[bootstrap] WARN: $pkg install failed (locker degrades)"
done

echo "[bootstrap] installing fonts + cursor/icon themes for qdshell rendering..."
# Without these packages, fc-list returns 0 entries and qdshell renders
# panel labels and tray icons as tofu (empty rectangle) glyphs; cursor
# theme load reports 0/36 sprites and applications get no pointer
# cursor. Idempotent; rpm -q gates the slow zypper path.
#
# `foot` ships /usr/share/applications/foot.desktop so the qdshell
# launcher index has at least one terminal at first toggle. weston
# packages weston-terminal but no .desktop file on Tumbleweed; the
# launcher index is built lazily and never rescans, so any terminal
# installed AFTER the first Ctrl+Space stays invisible until the
# session restarts (track via tasks/ — needs a `launcher-rescan` ctrl
# command).
for pkg in liberation-fonts dejavu-fonts \
           google-noto-sans-fonts google-noto-sans-mono-fonts \
           xcursor-themes adwaita-icon-theme \
           qt6-declarative-tools \
           foot; do
    rpm -q "$pkg" >/dev/null 2>&1 || \
        zypper -n install "$pkg" >/dev/null 2>&1 || \
        echo "[bootstrap] WARN: $pkg install failed (qdshell rendering degrades)"
done
fc-cache -f >/dev/null 2>&1 || true

echo "[bootstrap] adding admin to render+video groups (§6.8 dmabuf path)..."
# §6.8 dmabuf zero-copy: weston gl-renderer needs to open /dev/dri/renderD128
# (and card0) for GBM allocator init. Without these groups, both the outer
# RDP+gl compositor and the inner wayland-backend gl-renderer fall back
# silently to broken or pixman paths and zwp_linux_dmabuf_v1 advertises
# no formats (or no global at all on RDP). Idempotent.
usermod -aG render admin 2>/dev/null || true
usermod -aG video  admin 2>/dev/null || true

echo "[bootstrap] enabling linger for admin (user@1000 + dbus-broker)..."
# §6.6 S2: notifications / tray daemons need admin's user session bus
# up without a real login. loginctl enable-linger starts user@1000,
# which auto-starts dbus-broker and creates /run/user/1000/bus.
loginctl enable-linger admin
for i in 1 2 3 4 5 6 7 8; do
    [ -S /run/user/1000/bus ] && break
    sleep 1
done

echo "[bootstrap] provisioning §Phase-7 tier-3 silo users (user1=1001, user2=1002)..."
# §Phase-7 tier-3: cross-uid silos via waypipe. user1 is the canonical
# non-admin silo for the test suite; user2 is a second silo used by
# s37 to exercise multi-silo lifecycle. spec/02 row 3 (tier 3) requires
# at least one non-admin uid + a shared `qdistro-tier3` group whose
# members can connect to the bridge socket created by waypipe-client
# (which runs as admin). enable-linger so /run/user/<uid> + dbus-broker
# come up without a real login (silo apps need XDG_RUNTIME_DIR set).
getent group qdistro-tier3 >/dev/null || groupadd -r qdistro-tier3
usermod -aG qdistro-tier3 admin 2>/dev/null || true
for entry in user1:1001 user2:1002; do
    USER_NAME="${entry%:*}"
    USER_UID="${entry#*:}"
    if ! getent passwd "$USER_NAME" >/dev/null; then
        useradd -m -u "$USER_UID" -G qdistro-tier3 -s /bin/bash "$USER_NAME"
    fi
    usermod -aG qdistro-tier3 "$USER_NAME" 2>/dev/null || true
    # Standard test password from $QDISTRO_VM_PASSWORD.
    echo "$USER_NAME:${QDISTRO_VM_PASSWORD:?}" | chpasswd
    loginctl enable-linger "$USER_NAME"
    for i in 1 2 3 4 5 6 7 8; do
        [ -S "/run/user/${USER_UID}/bus" ] && break
        sleep 1
    done
done

echo "[bootstrap] installing qdistro-admin-broker..."
bash /root/sync-broker.sh
bash /root/install-broker-for-qdwin.sh

# qsu user-facing CLI + qdistro-root-exec service. Depends on the
# broker bus being up — install-qsu-for-vm.sh enables the .socket
# unit (socket-activated, so the .service comes up on first connect).
# Without this step, s58 (real qsu end-to-end) would have no
# /run/qdistro-root-exec/sock to connect to.
echo "[bootstrap] installing qsu + qdistro-root-exec..."
QSU_URL="$HOST/spike-6.5/qsu" bash /root/install-qsu-for-vm.sh

# §Phase-8 spec/13 password manager daemon (MVP slice). Depends on
# python313-cryptography being installed (install-deps.sh handles it
# via a soft check; if missing, the daemon fails at first connect and
# phase8 bats SKIPs).
echo "[bootstrap] installing qdistro-pwd..."
bash /root/install-pwd-for-vm.sh /root/pwd-src

# spec/13 admin polkit AuthenticationAgent — must come AFTER
# install-pwd-for-vm.sh because the agent's polkit-agent.conf maps
# com.qdistro.pwd.* to PAM, and the .policy file (com.qdistro.pwd.policy)
# is dropped by install-pwd-for-vm.sh. Order: pwd → polkit-agent so
# polkit's action introspection finds the action when the agent
# registers.
echo "[bootstrap] installing qdistro-polkit-agent..."
bash /root/install-polkit-agent-for-vm.sh /root/polkit-src

# §Phase-9 spec/20 print proxy skeleton (CUPS-in-VM transport stub).
# Service is enabled but not started — with no print VM yet the
# accept-then-fail-connect loop clutters journals. Tests start it
# explicitly; once Phase-9 §step 1 lands the service can flip to
# auto-start.
echo "[bootstrap] installing qdistro-print-proxy (skeleton)..."
bash /root/install-print-proxy-for-vm.sh /root/print-src

# §spec/14 Phase-8 MVP browser-bridge native-messaging host. Drops
# the bridge stub at /usr/lib/qdistro/browser-bridge plus the
# qdistro-browser-install admin CLI. No services to enable; the
# bridge is browser-launched on demand.
echo "[bootstrap] installing qdistro browser-bridge..."
bash /root/install-browser-bridge-for-vm.sh /root/browser-bridge-src

# §spec/17 §step 0 MVP recall ingest. Drops engine + reaper +
# CLI + SDK + per-user systemd unit/timer. Per-user dir creation
# is gated on `id admin` so a pre-user bake doesn't fail.
echo "[bootstrap] installing qdistro-recall..."
bash /root/install-recall-for-vm.sh /root/recall-src

# §spec/19 Phase-8 MVP — snapshot bridge engine + CLI + backup
# unit/timer. Idempotent; no Snapper config required (the unit
# ConditionPathExists-skips when /etc/qdistro/backup.conf is absent).
echo "[bootstrap] installing qdistro-snapshots..."
bash /root/install-snapshots-for-vm.sh /root/snapshots-src

# §spec/18 Phase-8 MVP — qdistro-phone daemon. Service stays masked
# until /etc/qdistro/phone/qdistro-phone.conf exists (admin pairs
# the first phone to flip it on).
echo "[bootstrap] installing qdistro-phone..."
bash /root/install-phone-for-vm.sh /root/phone-src

# §Phase-7 spec/30 step 7: audispd plugin → broker AVC ingestion.
# Install AFTER the broker so the parser module is already on the
# python path under /usr/libexec/qdistro/ (since 2026-04-29).
# Idempotent; SIGHUPs auditd to pick the descriptor up.
mkdir -p /root/qdistro-audisp
for f in qdistro-audisp-plugin qdistro-audisp.conf install.sh; do
    wget -q -O /root/qdistro-audisp/$f \
        "$HOST/qdistro-audisp/$f"
done
chmod +x /root/qdistro-audisp/install.sh /root/qdistro-audisp/qdistro-audisp-plugin
bash /root/qdistro-audisp/install.sh 2>&1 | tail -3 || \
    echo "[bootstrap] WARN: audisp plugin install failed (non-fatal)"

# §Phase-7 tier-2: build qdistro/tier2 container image (needs the
# host-installed qdwin-shell.so, so we run AFTER the meson install
# above). Skips when podman is absent.
if command -v podman >/dev/null 2>&1; then
    echo "[bootstrap] building qdistro/tier2:latest container image..."
    QDWIN_SRC="$SRC" bash /root/tier2/build-tier2-container.sh 2>&1 | tail -3
else
    echo "[bootstrap] podman not installed — skipping tier-2 image build"
fi
# bats setup() stops the broker for broker-absent scenarios
# (s3c-* / s5* / s4-broker-absent-*) and s4-revoke-teardown starts it
# explicitly. Leaving the service enabled+active at bootstrap end is
# the "production" default; the test suite manages transitions.

echo "[bootstrap] DONE — ready for bats suite"
