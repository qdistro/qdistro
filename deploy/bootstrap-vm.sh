#!/bin/bash
# Run inside the VM (as root) to finish Phase 1 bootstrap after packages
# are installed. Idempotent — safe to re-run.

set -euo pipefail

# 1. Ensure 'work' user exists (uid 2000, pw $QDISTRO_VM_PASSWORD).
# Phase 3 also needs 'work2' (uid 3000) so we can demo cross-user send-to.
if ! getent passwd work >/dev/null; then
  useradd -m -u 2000 -s /bin/bash work
  echo "work:${QDISTRO_VM_PASSWORD:?}" | chpasswd
fi
if ! getent passwd work2 >/dev/null; then
  useradd -m -u 3000 -s /bin/bash work2
  echo "work2:${QDISTRO_VM_PASSWORD:?}" | chpasswd
fi

# Linger for work + work2 so their systemd user manager (and hence
# their session bus + qdistro-user-relay) keep running without a
# graphical login. Without linger, /run/user/<uid>/bus evaporates
# as soon as the last pty closes and the broker can't reach the
# target uid's session at RelayMessage time.
loginctl enable-linger work  || true
loginctl enable-linger work2 || true

# 2. Ensure 'admin' has pw $QDISTRO_VM_PASSWORD (may already be set by baseweed)
echo "admin:${QDISTRO_VM_PASSWORD:?}" | chpasswd

# 3. LXQt session.conf pinning labwc as compositor (skip firstrun wizard)
install -d -o admin -g users /home/admin/.config/lxqt
install -o admin -g users -m 0644 /root/qdistro-deploy/deploy/lxqt-session.conf /home/admin/.config/lxqt/session.conf

# 4. greetd autologin to tty3 running startlxqtwayland as admin
install -o root -g root -m 0644 /root/qdistro-deploy/deploy/greetd-config.toml /etc/greetd/config.toml

# 4b. Install qdistro session wrapper scripts. greetd runs
# /usr/local/bin/qdistro-startlxqtwayland which pins labwc and runs
# lxqt-session through a wrapper that populates DBus activation env
# synchronously, suppressing the "DBus Environment wasn't updated"
# dialog and ensuring autostart apps see the right env.
install -m 0755 /root/qdistro-deploy/deploy/qdistro-startlxqtwayland.sh /usr/local/bin/qdistro-startlxqtwayland
install -m 0755 /root/qdistro-deploy/deploy/qdistro-lxqt-session-wrap.sh /usr/local/bin/qdistro-lxqt-session-wrap
install -m 0755 /root/qdistro-deploy/deploy/dismiss-dbus-warning.sh /usr/local/bin/qdistro-dismiss-dbus-warning

# Inject the auto-dismiss helper into labwc autostart (idempotent)
LABWC_AUTOSTART=/home/admin/.config/lxqt/labwc/autostart
if [ -f "$LABWC_AUTOSTART" ] && ! grep -q qdistro-dismiss-dbus-warning "$LABWC_AUTOSTART"; then
  TMPF=$(mktemp)
  {
    echo "/usr/local/bin/qdistro-dismiss-dbus-warning &"
    cat "$LABWC_AUTOSTART"
  } > "$TMPF"
  install -o admin -g users -m 0644 "$TMPF" "$LABWC_AUTOSTART"
  rm -f "$TMPF"
fi

# Reset stale autostart from earlier prefix attempt (idempotent — safe to re-run)
LABWC_AUTOSTART=/home/admin/.config/lxqt/labwc/autostart
if [ -f "$LABWC_AUTOSTART" ] && head -1 "$LABWC_AUTOSTART" | grep -q '^#!/bin/sh'; then
  # Strip our prefix block (lines through the second blank-or-comment after our marker)
  sed -i '/^# Runs synchronously at the start of the labwc autostart/,/^systemctl --user import-environment/d' "$LABWC_AUTOSTART"
fi

# 5. Broker: code, dbus policy, systemd unit
install -d -m 0700 /var/lib/qdistro/approvals
install -d -m 0700 /var/lib/qdistro/audit

# Broker code lives in a root-owned path so a compromised user uid (incl. admin)
# can't replace it and gain root on next service restart.
# qdistro_admin_broker.py is mode 0755 — it's the systemd ExecStart target so
# the kernel's execve hook reads its SELinux label (qdistro_broker_exec_t per
# broker-policy/ Phase 2) and triggers the init_daemon_domain transition into
# qdistro_broker_t. The other modules are import-only so they stay 0644.
# Path is /usr/libexec/qdistro/ since 2026-04-29 — outside Tumbleweed's
# `/usr/(.*/)?lib(/.*)?` lib_t glob so .fc wins natively.
install -d -o root -g root -m 0755 /usr/libexec/qdistro
install -o root -g root -m 0755 /root/qdistro-deploy/broker/qdistro_admin_broker.py /usr/libexec/qdistro/
install -o root -g root -m 0644 /root/qdistro-deploy/broker/qdistro_admin_cache.py /usr/libexec/qdistro/
install -o root -g root -m 0644 /root/qdistro-deploy/broker/qdistro_admin_audit.py /usr/libexec/qdistro/
install -o root -g root -m 0644 /root/qdistro-deploy/broker/qdistro_admin_ratelimit.py /usr/libexec/qdistro/
install -o root -g root -m 0644 /root/qdistro-deploy/broker/qdistro_admin_rules.py /usr/libexec/qdistro/

# /usr/local/lib/qdistro/ stays as the home for the rest of qdistro's
# python (qsu, polkit, user_relay, stubs, TUI, sendto plugins,
# qterminator-src, qnotebook-src). Those don't carry a SELinux .fc
# label so the lib_t shadowing isn't a concern for them.
install -d -o root -g root -m 0755 /usr/local/lib/qdistro

# qsu — sudo replacement. Root-exec service runs as root and brokers
# every call; the qsu client is a thin user-side shim.
install -o root -g root -m 0644 /root/qdistro-deploy/qsu/qdistro_root_exec.py /usr/local/lib/qdistro/
install -o root -g root -m 0644 /root/qdistro-deploy/qsu/qsu.py /usr/local/lib/qdistro/qsu.py
cat > /usr/local/bin/qsu <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/qdistro/qsu.py "$@"
EOF
chmod 0755 /usr/local/bin/qsu
install -o root -g root -m 0644 /root/qdistro-deploy/qsu/qdistro-root-exec.service /etc/systemd/system/qdistro-root-exec.service
install -o root -g root -m 0644 /root/qdistro-deploy/qsu/qdistro-root-exec.socket  /etc/systemd/system/qdistro-root-exec.socket

# qdistro polkit agent — session-scoped, takes over from the default
# LXQt polkit agent so all polkit auth prompts route through the
# qdistro broker.
install -o root -g root -m 0644 /root/qdistro-deploy/polkit/qdistro_polkit_agent.py /usr/local/lib/qdistro/
install -o root -g root -m 0644 /root/qdistro-deploy/polkit/qdistro-polkit-agent.service /etc/systemd/user/qdistro-polkit-agent.service
# Disable the default LXQt policykit agent for admin; two session
# agents fight over RegisterAuthenticationAgent.
runuser -u admin -- systemctl --user disable lxqt-policykit-agent.service 2>/dev/null || true
runuser -u admin -- systemctl --user mask lxqt-policykit-agent.service 2>/dev/null || true
runuser -u admin -- systemctl --user enable qdistro-polkit-agent.service 2>/dev/null || true

install -m 0755 /root/qdistro-deploy/cli/qdistro_approvals.py /usr/local/sbin/qdistro-approvals

# TUI approver: code under /usr/local/lib/qdistro/tui (root:root 0644);
# launcher /usr/local/bin/qdistro-admin-tui that any user can run
install -d -o root -g root -m 0755 /usr/local/lib/qdistro/tui
install -o root -g root -m 0644 /root/qdistro-deploy/tui/__init__.py /usr/local/lib/qdistro/tui/
install -o root -g root -m 0644 /root/qdistro-deploy/tui/qdistro_admin_tui.py /usr/local/lib/qdistro/tui/
install -o root -g root -m 0644 /root/qdistro-deploy/tui/broker_client.py /usr/local/lib/qdistro/tui/
install -o root -g root -m 0644 /root/qdistro-deploy/tui/silo_colors.py /usr/local/lib/qdistro/tui/
cat > /usr/local/bin/qdistro-admin-tui <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/qdistro/tui/qdistro_admin_tui.py "$@"
EOF
chmod 0755 /usr/local/bin/qdistro-admin-tui

# User-side bits (admin app + tests) stay under admin's home as before
install -d /home/admin/qdistro/admin_app /home/admin/qdistro/tests
install -o admin -g users -m 0644 /root/qdistro-deploy/admin_app/qdistro_admin_app.py /home/admin/qdistro/admin_app/
install -o admin -g users -m 0644 /root/qdistro-deploy/admin_app/qdistro-admin-app.desktop /home/admin/qdistro/admin_app/qdistro-admin-app.desktop
install -o admin -g users -m 0644 /root/qdistro-deploy/tests/test_permission.py /home/admin/qdistro/tests/
chown -R admin:users /home/admin/qdistro

# Manual-test launcher for the admin app, installed to a path admin can
# read/execute. GUI test scenarios and hands-on debugging both call it;
# /root/qdistro-deploy/ is not traversable by non-root users.
install -o root -g root -m 0755 /root/qdistro-deploy/deploy/start-admin-app.sh \
    /usr/local/bin/qdistro-start-admin-app
install -o root -g root -m 0755 /root/qdistro-deploy/deploy/start-admin-tui.sh \
    /usr/local/bin/qdistro-start-admin-tui

install -m 0644 /root/qdistro-deploy/broker/com.qdistro.AdminBroker1.conf /etc/dbus-1/system.d/com.qdistro.AdminBroker1.conf
# spec/30 §"dbus-broker policy-reload mystery": defensive oneshot
# that reloads dbus-broker before our broker activates, closing a
# flaky-on-first-baked-boot AccessDenied path. Idempotent.
if [ -f /root/qdistro-deploy/broker/qdistro-dbus-reload.service ]; then
    install -m 0644 /root/qdistro-deploy/broker/qdistro-dbus-reload.service \
        /etc/systemd/system/qdistro-dbus-reload.service
fi
install -m 0644 /root/qdistro-deploy/user_relay/com.qdistro.UserRelay.conf /etc/dbus-1/system.d/com.qdistro.UserRelay.conf
install -m 0644 /root/qdistro-deploy/broker/qdistro-admin-broker.service /etc/systemd/system/qdistro-admin-broker.service

# Clean up the old user-home broker location if a previous bootstrap put it there
rm -rf /home/admin/qdistro/broker /home/admin/qdistro/sdk

# 6. SDK on system-wide sys.path so any user can import qdistro_app
install -d /usr/lib/python3.13/site-packages/qdistro_app
install -m 0644 /root/qdistro-deploy/sdk/qdistro_app/__init__.py /usr/lib/python3.13/site-packages/qdistro_app/__init__.py

# 6b. Phase 3 user-relay daemon + stub apps.
# Relay code lives alongside the broker under /usr/local/lib/qdistro/
# (root-owned) so users can execute it but not modify it. Stubs too —
# while they're "stubs", they run in user sessions and relay messages
# into other users, so the same containment applies.
install -o root -g root -m 0644 /root/qdistro-deploy/user_relay/qdistro_user_relay.py /usr/local/lib/qdistro/
install -d -o root -g root -m 0755 /usr/local/lib/qdistro/stubs
install -o root -g root -m 0644 /root/qdistro-deploy/stubs/qstub_sender.py  /usr/local/lib/qdistro/stubs/
install -o root -g root -m 0644 /root/qdistro-deploy/stubs/qstub_notepad.py /usr/local/lib/qdistro/stubs/

cat > /usr/local/bin/qstub-sender <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/qdistro/stubs/qstub_sender.py "$@"
EOF
chmod 0755 /usr/local/bin/qstub-sender

cat > /usr/local/bin/qstub-notepad <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /usr/local/lib/qdistro/stubs/qstub_notepad.py "$@"
EOF
chmod 0755 /usr/local/bin/qstub-notepad

# User-unit files go in /etc/systemd/user so `systemctl --global
# enable` picks them up for every user (existing + future).
install -o root -g root -m 0644 /root/qdistro-deploy/user_relay/qdistro-user-relay.service \
    /etc/systemd/user/qdistro-user-relay.service
install -o root -g root -m 0644 /root/qdistro-deploy/stubs/qstub-notepad.service \
    /etc/systemd/user/qstub-notepad.service

# Enable for every user; takes effect at the user manager's next start.
systemctl --global enable qdistro-user-relay.service
systemctl --global enable qstub-notepad.service

# Restart the work/work2 user managers now so they pick up the new
# units without needing a reboot. With linger enabled, `systemctl
# start user@<uid>.service` is enough to ensure systemd --user is
# running; --machine=<user>@.host --user then reaches that instance
# from root without needing to fix up XDG_RUNTIME_DIR by hand.
for u in work work2; do
  uid=$(id -u "$u")
  systemctl start "user@${uid}.service" || true
  # Wait for the user bus socket to appear (up to 5s) — user@.service
  # returns before /run/user/<uid>/bus is fully listenable on a cold
  # start.
  for _ in 1 2 3 4 5; do
    [ -S "/run/user/${uid}/bus" ] && break
    sleep 1
  done
  systemctl --machine="${u}@.host" --user daemon-reload || true
  systemctl --machine="${u}@.host" --user restart qdistro-user-relay.service || true
  systemctl --machine="${u}@.host" --user restart qstub-notepad.service     || true
done

# 7. Autostart admin app in admin's LXQt session
install -d -o admin -g users /home/admin/.config/autostart
install -o admin -g users -m 0644 /root/qdistro-deploy/admin_app/qdistro-admin-app.desktop /home/admin/.config/autostart/qdistro-admin-app.desktop

# 7b. Pin qterminal's default geometry to 1200x700 so the TUI subtitle
# (scope warning chunk) and the full keybinding footer fit without
# truncation. Documented in tests/integration/permissions-gui/AGENTS.md §7.
install -d -o admin -g users /home/admin/.config/qterminal.org
cat > /home/admin/.config/qterminal.org/qterminal.ini <<'INI'
[General]
HideTabBarWithOneTab=false
TerminalTransparency=0
version=2.3.0

[MainWindow]
size=@Size(1200 700)
pos=@Point(40 40)
SaveSizeOnExit=false
SavePosOnExit=false
INI
chown admin:users /home/admin/.config/qterminal.org/qterminal.ini

# 8. Phase 1 test script in a spot 'work' can reach (drop to /tmp at test time)
install -m 0755 /root/qdistro-deploy/tests/test_permission.py /usr/local/bin/qdistro-test-permission

# ====================================================================
# Phase 4 — real qterminator + qnotebook under SDK via plugins
# ====================================================================
# This section is idempotent with Phase 3: re-running bootstrap on a
# Phase-3 clone adds the Phase-4 pieces without disturbing the stubs.
# The Phase-3 scenarios (11-14) continue to work against the stub
# notepad; Phase-4 scenarios (15+) exercise the real apps instead.

# 10. qdistro-users group + xhost SI allowlist for cross-uid display.
# Decision recorded in : the expedient
# is `xhost +si:localuser:work +si:localuser:work2` from admin's labwc
# autostart. Future work: spec/03 nested compositors. Explicit loud
# caveat: any uid in the allowlist can XTest any other — NOT the
# production model.
if ! getent group qdistro-users >/dev/null; then
    groupadd qdistro-users
fi
for u in admin work work2; do
    if getent passwd "$u" >/dev/null; then
        usermod -aG qdistro-users "$u" || true
    fi
done

install -o root -g root -m 0755 /root/qdistro-deploy/deploy/start-user-app.sh \
    /usr/local/bin/qdistro-start-user-app

# 10a. Install xhost client.
zypper -n install xhost >/dev/null 2>&1 || true

# 10b. Inject xhost call into labwc autostart (idempotent — grep-guarded).
# Runs after XWayland is up so DISPLAY=:0 is valid. Phase 4 scenarios
# assume it ran; the xhost call itself is a no-op if DISPLAY is absent.
LABWC_AUTOSTART=/home/admin/.config/lxqt/labwc/autostart
if [ -f "$LABWC_AUTOSTART" ] && ! grep -q 'qdistro-xhost-allow' "$LABWC_AUTOSTART"; then
    TMPF=$(mktemp)
    {
        cat "$LABWC_AUTOSTART"
        echo ''
        echo '# qdistro-xhost-allow — cross-uid display expedient (Phase 4).'
        echo '# See  for the tradeoffs.'
        echo '(sleep 1 && xhost +si:localuser:work +si:localuser:work2 >/dev/null 2>&1) &'
    } > "$TMPF"
    install -o admin -g users -m 0644 "$TMPF" "$LABWC_AUTOSTART"
    rm -f "$TMPF"
fi

# 11. qterminator — source tree + runtime + QTermWidget Python bindings.
# Source was unpacked to /usr/local/lib/qdistro/qterminator-src/ by the host-side deploy
# (deploy/deploy-to-vm.sh). We don't touch /usr/local/lib/qdistro/qterminator-src
# beyond building; the launcher shim points PYTHONPATH at it.
if [ -d /usr/local/lib/qdistro/qterminator-src ]; then
    # Runtime deps.
    zypper -n install \
        qtermwidget-devel libqtermwidget6-2 qtermwidget-data \
        python313-pyqt-builder python313-pyqt-builder-doc \
        sip-tools \
        >/dev/null 2>&1 || true

    # Build SIP bindings if they're not already present. The wheel
    # goes into /usr/local/lib/qdistro/qterminator-src/qtermwidget-pyqt/build/; we leave
    # it in place and point PYTHONPATH at the package dir so the
    # import `from QTermWidget import QTermWidget` finds it.
    if ! python3 -c 'from QTermWidget import QTermWidget' 2>/dev/null; then
        # The justfile invokes util/build-sip.sh which runs sip-build
        # and places the .so into qtermwidget-pyqt/build/. Invoke
        # directly — no Java-level `just` needed in the VM.
        if [ -x /usr/local/lib/qdistro/qterminator-src/util/build-sip.sh ]; then
            (cd /usr/local/lib/qdistro/qterminator-src && bash util/build-sip.sh) \
                > /tmp/qterminator-build.log 2>&1 || {
                    echo "[bootstrap-vm] QTermWidget SIP build failed — see /tmp/qterminator-build.log"
                    echo "[bootstrap-vm] Phase 4 apps won't launch until this is fixed."
                }
        fi
    fi

    # Launcher: PYTHONPATH covers both the qterminator source tree and
    # the built QTermWidget binding.
    cat > /usr/local/bin/qterminator <<'EOF'
#!/bin/sh
# Launcher for qterminator + QTermWidget python bindings installed from source.
QTERM_BUILD="$(ls -d /usr/local/lib/qdistro/qterminator-src/qtermwidget-pyqt/build/lib.* 2>/dev/null | head -1 || true)"
export PYTHONPATH="/usr/local/lib/qdistro/qterminator-src${QTERM_BUILD:+:$QTERM_BUILD}${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m qterminator "$@"
EOF
    chmod 0755 /usr/local/bin/qterminator
fi

# 12. qnotebook — pure Python, no build step.
if [ -d /usr/local/lib/qdistro/qnotebook-src ]; then
    # markdown-it-py is the parser dep; PyQt6 is already in base deps
    # since the stubs use it.
    zypper -n install python313-markdown-it-py >/dev/null 2>&1 || true

    cat > /usr/local/bin/qnotebook <<'EOF'
#!/bin/sh
export PYTHONPATH="/usr/local/lib/qdistro/qnotebook-src${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m zim_qt "$@"
EOF
    chmod 0755 /usr/local/bin/qnotebook
fi

# 13. Drop the two plugins into a neutral root-owned location; symlink
# into each participating uid's plugin dir so users can't swap them
# for something malicious.
install -d -o root -g root -m 0755 /usr/local/lib/qdistro/plugins
install -o root -g root -m 0644 /root/qdistro-deploy/qdistro_plugins/qdistro_sendto_qterminator.py \
    /usr/local/lib/qdistro/plugins/qdistro_sendto_qterminator.py
install -o root -g root -m 0644 /root/qdistro-deploy/qdistro_plugins/qdistro_sendto_qnotebook.py \
    /usr/local/lib/qdistro/plugins/qdistro_sendto_qnotebook.py

# 13a. qterminator plugin goes into work's per-user plugin dir. The
# user plugin dir is ~/.config/qterminator/plugins/; qterminator's
# PluginManager auto-discovers from there (see qterminator/plugin.py
# PLUGIN_DIRS). Symlink so a fix lands the next time qterminator
# restarts without a reinstall.
install -d -o work -g work /home/work/.config
install -d -o work -g work /home/work/.config/qterminator
install -d -o work -g work /home/work/.config/qterminator/plugins
ln -sf /usr/local/lib/qdistro/plugins/qdistro_sendto_qterminator.py \
    /home/work/.config/qterminator/plugins/qdistro_sendto.py
chown -h work:work /home/work/.config/qterminator/plugins/qdistro_sendto.py || true

# 13b. qnotebook plugin lives as a builtin (shipped with the source).
# That makes it discoverable on every notebook work2 opens without
# per-notebook setup. Enable-key = "qdistro_sendto" (stem of the
# filename). QSettings below pre-enables it for work2.
if [ -d /usr/local/lib/qdistro/qnotebook-src/zim_qt/plugins/builtin ]; then
    install -o root -g root -m 0644 /root/qdistro-deploy/qdistro_plugins/qdistro_sendto_qnotebook.py \
        /usr/local/lib/qdistro/qnotebook-src/zim_qt/plugins/builtin/qdistro_sendto.py
fi

# 14. Seed work2's test notebook + preference file so qnotebook boots
# straight into a usable state with the plugin enabled.
install -d -o work2 -g work2 -m 0755 /home/work2/testnb
if [ ! -f /home/work2/testnb/Home.md ]; then
    cat > /tmp/home.md <<'EOF'
# Home

Test notebook for Phase-4 cross-user send-to. Payloads from
qterminator should land below:

---
EOF
    install -o work2 -g work2 -m 0644 /tmp/home.md /home/work2/testnb/Home.md
    rm -f /tmp/home.md
fi

install -d -o work2 -g work2 -m 0755 /home/work2/.config
install -d -o work2 -g work2 -m 0755 /home/work2/.config/zim-qt

# QSettings INI format requires lists to be serialised as @Variant
# byte blobs (native QVariant serialisation); a plain value like
# `plugins_enabled=qdistro_sendto` gets read back as a string, and
# type=list coercion returns the character-split list (["q","d","i"...]).
# Write the conf via QSettings itself so the byte encoding matches what
# qnotebook's QSettings reads.
runuser -u work2 -- env QT_QPA_PLATFORM=offscreen python3 - <<'PY'
from PyQt6.QtCore import QSettings
s = QSettings("zim-qt", "zim-qt")
s.setValue("last_notebook", "/home/work2/testnb")
s.setValue("plugins_enabled", ["qdistro_sendto"])
s.setValue("dark_mode", False)
s.setValue("spell_enabled", False)
# Versioning calls git init on every open — bootstrap-vm has git-core
# so leave it on (matches qnotebook's default).
s.setValue("session_restore_enabled", False)
s.setValue("autosave_enabled", True)
s.setValue("autosave_ms", 30000)
s.sync()
PY

# 15. Install the qdistro_app SDK so work+work2 python can import it.
# Already covered by §6 above — no extra install. Re-assert permissions
# so a rerun doesn't leave a file the plugin can't read.
chmod -R a+rX /usr/lib/python3.13/site-packages/qdistro_app

# 9. Reload systemd + dbus policy; enable services; set graphical target.
# The Tumbleweed base image ships with /etc/systemd/system/display-manager.service
# symlinked to display-manager-legacy.service; `systemctl enable greetd.service`
# refuses to clobber it. Remove first so the enable lands.
systemctl daemon-reload
systemctl reload dbus-broker.service || systemctl reload dbus.service || true
rm -f /etc/systemd/system/display-manager.service
systemctl enable qdistro-dbus-reload.service 2>/dev/null || true
systemctl enable qdistro-admin-broker.service
systemctl enable qdistro-root-exec.socket
systemctl enable greetd.service
systemctl set-default graphical.target

echo "[bootstrap-vm] done"
