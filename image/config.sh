#!/bin/bash
# qdistro kiwi config script.
#
# Runs inside the image chroot AFTER packages install, BEFORE the
# rootfs is packed into the OEM disk image.
#
# Layout assumed (build.sh rsyncs these in via the `root/` overlay):
#   /root/qdistro-src/qdistro/
#   /root/qdistro-src/qdwin/
#   /root/qdistro-src/qdshell/
#
# This is the same /root/qdistro-src layout that
# qdistro/scripts/vm/fresh-vm-bootstrap.sh expects, so we reuse the
# project's own installers (install-broker-for-qdwin.sh etc.) verbatim
# rather than re-implement them.

set -euxo pipefail

. /.kconfig
. /.profile

echo "[qdistro-image] kiwi config.sh: $kiwi_iname-$kiwi_iversion"

SRC=/root/qdistro-src
QD="$SRC/qdistro"

# The build profile, validated ONCE: dev (passwordless sudo, the tester
# image) or release (the safe default). Every later gate -- sudoers, the
# release stamp -- reads this variable, so the accepted set is stated here
# and nowhere else; build-in-vm.sh applies the same check on the host.
QDISTRO_IMAGE_PROFILE="${QDISTRO_PROFILE:-release}"
case "$QDISTRO_IMAGE_PROFILE" in
    dev|release) ;;
    *) echo "[qdistro-image] FATAL: QDISTRO_PROFILE must be dev or release, got: $QDISTRO_IMAGE_PROFILE" >&2; exit 1 ;;
esac
# State it unmissably in the build log, and state what it DECIDES. The profile
# is not cosmetic: it selects the passwordless sudoers rule and the SELinux
# runtime mode written into /etc/selinux/config below. build-in-vm.sh asserts
# this same value back out of the finished raw (image/lib/profile-proof.sh).
echo "[qdistro-image] ============================================================"
if [ "$QDISTRO_IMAGE_PROFILE" = dev ]; then
    echo "[qdistro-image]  PROFILE = dev      (the TESTER image)"
    echo "[qdistro-image]    -> passwordless admin sudoers baked"
    echo "[qdistro-image]    -> SELINUX=permissive in /etc/selinux/config"
else
    echo "[qdistro-image]  PROFILE = release  (the safe default)"
    echo "[qdistro-image]    -> no passwordless sudoers"
    echo "[qdistro-image]    -> SELINUX=enforcing in /etc/selinux/config"
fi
echo "[qdistro-image] ============================================================"

if [ -f /etc/os-release.qdistro ]; then
    rm -f /etc/os-release
    mv /etc/os-release.qdistro /etc/os-release
fi

# /etc/qdistro/release: what this image was built from (todo/iso/14 Phase C).
# build.sh strips .git while syncing the five source repos into the overlay,
# so the commits can only be read on the host at sync time; sync_sources
# writes them, with the Tumbleweed snapshot id the repositories are pinned
# to, into /root/qdistro-source-manifest. Version comes from kiwi's own
# /.profile and must agree with os-release; profile is this build's.
# FATAL if the manifest is missing or short: an image that cannot say what
# went in is not a tester image, and a bug report needs these lines.
# kiwi imports only the description's scripts into the chroot, not lib/; the
# synced qdistro source tree carries the same file, from the same checkout.
. "$QD/image/lib/release-stamp.sh"
if ! qdistro_write_release /root/qdistro-source-manifest /etc/os-release \
        /etc/qdistro/release "$kiwi_iversion" "$QDISTRO_IMAGE_PROFILE"; then
    echo "[qdistro-image] FATAL: could not write /etc/qdistro/release. Aborting build." >&2
    exit 1
fi
rm -f /root/qdistro-source-manifest
echo "[qdistro-image] /etc/qdistro/release:"
sed 's/^/[qdistro-image]   /' /etc/qdistro/release

# kiwi does not persist description <repository> entries into the packed
# image (iso/14 Phase G.2: /etc/zypp/repos.d is empty). Bootstrap still
# zyppers. Write the same history/<snapshot>/ URLs config.xml pinned.
. "$QD/image/lib/snapshot-repos.sh"
if ! qdistro_write_snapshot_repos /etc/qdistro/release; then
    echo "[qdistro-image] FATAL: could not write snapshot zypper repos. Aborting build." >&2
    exit 1
fi
echo "[qdistro-image] snapshot zypper repos:"
sed 's/^/[qdistro-image]   /' /etc/zypp/repos.d/qdistro-snapshot-oss.repo /etc/zypp/repos.d/qdistro-snapshot-nonoss.repo

# jeos-firstboot fights us for tty1 and blocks multi-user.target on
# openSUSE JeOS-derived images. Mask before greetd takes over.
systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true

echo "qdistro" > /etc/hostname

ssh-keygen -A
# sshd is intentionally NOT enabled by default. A network-reachable sshd
# combined with the baked default password is a remote default-credential
# exposure (Opus security review finding #1). Host keys are generated above so
# the VM test harness can start sshd on demand over the qemu-guest-agent channel
# (see image/verify.sh); for human dev VMs login is greetd autologin on the
# console, which never needs sshd.
systemctl enable NetworkManager.service
# qemu-guest-agent: out-of-band host->guest control channel (virtio-serial,
# no network). Lets the VM test harness drive the guest (e.g. start sshd) over
# `virsh qemu-agent-command` without baking network-reachable SSH on by default.
systemctl enable qemu-guest-agent.service
# openSUSE's packaged default (/usr/etc/sysconfig/qemu-ga) passes
# --block-rpcs=guest-exec,guest-exec-status, so the agent answered
# guest-ping and guest-file-* but refused the one call verify.sh needs to
# start sshd (every Phase A-C verify.sh run died at that baseline). The
# admin override file the unit reads after the vendor default
# (EnvironmentFile=-/etc/sysconfig/qemu-ga, expanded into ExecStart as
# ${FILTER_RPC_ARGS}; image/verify-contents.sh pins both lines of the unit
# and that the vendor list is exactly those two RPCs, so "" is the vendor
# list minus them). Exposure: none new -- the agent listens only on the
# virtio-serial port, which exists only when a hypervisor created the VM,
# and that hypervisor already holds the disk. On real hardware the unit's
# BindsTo= device never appears and the agent does not run.
install -d -m 0755 /etc/sysconfig
cat > /etc/sysconfig/qemu-ga <<'EOF'
# qdistro image: allow every guest-agent RPC (the vendor default blocks
# guest-exec/guest-exec-status). image/verify.sh drives the booted VM with
# guest-exec; the channel is hypervisor-only (virtio-serial). See config.sh.
FILTER_RPC_ARGS=""
EOF
chmod 0644 /etc/sysconfig/qemu-ga

# Sudoers policy is profile-gated. This config.sh bakes a RELEASE image by
# default, which must NOT ship `admin ALL=(ALL) NOPASSWD: ALL` — a baked-in
# passwordless-root rule on every shipped disk is exactly the escape hatch the
# hardening review flagged. Cross-uid privileged actions on a release image go
# through qsu / the broker's scoped approval; admin keeps password-required
# sudo via wheel membership. Set QDISTRO_PROFILE=dev when baking a disposable
# developer image to restore the passwordless rule.
if [ "$QDISTRO_IMAGE_PROFILE" = dev ]; then
    install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'
    echo "[qdistro-image] WARN: dev profile — baked passwordless sudoers (admin NOPASSWD: ALL); NOT for release"
else
    rm -f /etc/sudoers.d/99-admin
    echo "[qdistro-image] release profile: no passwordless sudoers baked (admin uses password-required sudo; cross-uid via qsu/broker)"
fi

# Build the three sibling projects out of /root/qdistro-src/.
echo "[qdistro-image] building qdwin..."
cd "$SRC/qdwin"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

echo "[qdistro-image] building qdistro daemons..."
cd "$QD/daemons"
meson setup build --wipe --prefix=/usr
meson compile -C build
meson install -C build

# qdshell's qml-plugin binds qdwin_shell_v1 -> QML. Without it the
# install-qdwin-session installer falls back to stubs and qdshell.service
# can't resolve `import Qdistro.Qdwin 1.0`.
echo "[qdistro-image] building qdshell qml-plugin..."
cd "$SRC/qdshell"
meson setup build --wipe --prefix=/usr
meson compile -C build

cd "$QD"
# ---------------------------------------------------------------------------
# The installer chain: ONE chain, the bootstrap's (todo/iso/14 Phase D).
#
# config.sh used to carry its own INSTALLERS array, and todo/iso/03 found the
# two lists installing different products (the image had media, multimachine
# and recall, which the bootstrap does not install; it lacked sdk and the
# whole isolation ladder above tier 2). Now the image runs the bootstrap's
# own chain functions, so what a tester's stick installs and what
# qdistro-bootstrap.sh installs on a machine are the same list by
# construction, and the list is stated once (installer_chain_entries;
# `qdistro-bootstrap.sh --list-steps` prints it).
#
# Sourcing contract (documented at the top of the bootstrap): sourcing runs
# its globals, which are (re)initialised from their QDISTRO_* environment
# forms, so those are what we set -- never the internal names (REPO_ROOT,
# STRICT, QDISTRO_STATE_DIR is both), which the source would clobber.
#   QDISTRO_REPO_ROOT   the synced sources; the chain runs $REPO_ROOT/qdistro
#   QDISTRO_PROFILE     dev|release, the validated image profile: `phone` is
#                       a dev-only step (decision D4) and follows the same
#                       guard here as on a machine install; in release the
#                       bootstrap's source-tree trust gate also runs (the
#                       synced tree is root-owned, mode 0755/0644).
#   QDISTRO_STRICT=1    every chain step is FATAL on failure (the bootstrap
#                       default is warn-and-continue); an image with a
#                       missing installer is not a tester image.
#   QDISTRO_STATE_DIR   the resume state file, ON the image: each succeeded
#                       step is recorded in installer-chain.state, so the
#                       booted system can `qdistro-bootstrap.sh --resume`,
#                       verify-contents.sh can diff the record against the
#                       chain (the Phase D DONE bar), and the bootstrap's own
#                       end-of-run completeness check (iso2 02 F1) has the
#                       evidence it judges -- fatal here regardless of
#                       profile, via STRICT.
# Offline-install contract (Phase B): every installer in the chain sources
# scripts/install/lib/qdistro-offline.sh. With this flag set AND the root
# corroborated as a chroot (positive evidence: kiwi bind-mounts the builder's
# /proc here first), file drops and `systemctl enable` run as normal while
# every operation that needs a running system manager or bus (`start`,
# `daemon-reload`, `busctl`, `loginctl`, readiness probes) is skipped with a
# logged "[offline] skipped" line. Any other non-zero exit is a real failure.
#
# Deliberately NOT installed, because the bootstrap chain does not install
# them: recall (cut from v1, decision D2; its installer refuses without
# QDISTRO_ENABLE_POSTV1_RECALL=1), media and multimachine (audit
# recommendation DEMOTE, fable-release/13 rows 11d/11e; never promoted into
# the chain), the admin approval-queue TUI (admin_app/, tui/; neither chain
# has ever installed it). verify-contents.sh asserts their artefacts are
# absent. What IS installed is stated in image/AGENTS.md ("What the chain
# installs").
export QDISTRO_REPO_ROOT="$SRC"
export QDISTRO_PROFILE="$QDISTRO_IMAGE_PROFILE"
export QDISTRO_STRICT=1
export QDISTRO_STATE_DIR=/var/lib/qdistro/bootstrap
export QDISTRO_OFFLINE_INSTALL=1
# shellcheck source=../scripts/install/qdistro-bootstrap.sh
. "$QD/scripts/install/qdistro-bootstrap.sh"
# The source redefined log/warn/die (die exits 1: fatal, as every step of
# this script is) and set REPO_ROOT/STRICT/QDISTRO_STATE_DIR from the
# exports above. Assert that before running anything as root from them.
if [ "$REPO_ROOT" != "$SRC" ] || [ "$STRICT" != 1 ] \
        || [ "$QDISTRO_STATE_DIR" != /var/lib/qdistro/bootstrap ]; then
    echo "[qdistro-image] FATAL: bootstrap globals did not take the exported values" \
         "(REPO_ROOT=$REPO_ROOT STRICT=$STRICT QDISTRO_STATE_DIR=$QDISTRO_STATE_DIR). Aborting build." >&2
    exit 1
fi
# resolve_profile validates the exported profile (the canonical names this
# script admits are fixed points of it; the alias forms were refused above).
resolve_profile || { echo "[qdistro-image] FATAL: bootstrap rejected QDISTRO_PROFILE=$QDISTRO_PROFILE" >&2; exit 1; }
echo "[qdistro-image] installer chain (bootstrap's, profile=$QDISTRO_PROFILE, strict, offline): $(installer_chain_names | tr '\n' ' ')"
# Runs every chain step through run_installer_step (fatal under STRICT) and
# then chain_completeness_check: the recorded steps must equal the chain
# minus dev-only steps outside dev, or the build dies.
install_python_modules
echo "[qdistro-image] installer chain recorded on the image:"
sed 's/^/[qdistro-image]   /' /var/lib/qdistro/bootstrap/installer-chain.state

# Kiwi sources only the installer chain, not bootstrap main()'s SELinux
# setup. Configure the on-disk mode explicitly; never setenforce in a chroot.
. "$QD/image/lib/selinux-mode.sh"
qdistro_image_selinux_mode /etc/selinux/config "$QDISTRO_IMAGE_PROFILE" || {
    echo "[qdistro-image] FATAL: could not establish SELinux profile mode" >&2
    exit 1
}
for pol in selinux/broker selinux/pwd selinux/session_manager selinux/tier1; do
    if [ -d "$QD/$pol" ] && [ -x "$QD/$pol/install-policy.sh" ]; then
        # tier1 module references types defined by broker module; if it
        # loads first the AST resolves on the second pass at boot time.
        # Fatal (Phase B): a service shipped without its policy module is an
        # incomplete image, and the fail-open form hid exactly that class of
        # defect in the installer chain above.
        if ! (cd "$QD/$pol" && bash install-policy.sh); then
            echo "[qdistro-image] FATAL: $pol policy install failed. Aborting build." >&2
            exit 1
        fi
    fi
done

# install-qdwin-session-for-vm.sh honours the same offline contract
# (linger marker written directly; user-unit wants-symlinks written
# directly). QDWIN_SESSION_AUTOSTART=0: in the greeter image
# qdwin-session.target must NOT be pulled in by default.target -- the
# greeter's qdwin-session-launcher starts it explicitly after PAM auth, and
# an auto-started target would race it for the wayland-1 socket. (This
# replaces the loginctl/runuser shims that used to be interposed on PATH.)
export QDWIN_SESSION_AUTOSTART=0
# install-qdwin-session-for-vm.sh does `usermod -aG ...,seat admin`, assuming the
# `seat` group already exists (its comment says fresh-vm-bootstrap.sh creates it).
# The kiwi image build never runs fresh-vm-bootstrap.sh, so create the libseat
# `seat` group here first or the usermod aborts config.sh (set -e).
getent group seat >/dev/null || groupadd -r seat
# Production image: the shell-capture authority must never be baked in. The
# installer only emits it when the caller exports QDWIN_ENABLE_SHELL_CAPTURE,
# but unset it explicitly so an operator's inherited test environment cannot
# leak it into a shipped unit.
unset QDWIN_ENABLE_SHELL_CAPTURE
bash "$QD/scripts/install/install-qdwin-session-for-vm.sh" "$SRC/qdshell"

install -d -m 0755 /etc/greetd

# P01 boot path: qdgreeter on tty3 (the production session). _greeter
# system user owns the unprivileged greeter process; PAM does the
# privilege handoff at start_session time.
if ! getent passwd _greeter >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin _greeter || true
else
    usermod --shell /usr/sbin/nologin --home /nonexistent _greeter 2>/dev/null || true
fi
for g in video render input tty; do
    getent group "$g" >/dev/null && usermod -aG "$g" _greeter || true
done

install -m 0644 "$QD/deploy/greetd-config.toml"          /etc/greetd/config.toml

# systemd hardening drop-in for the distro-packaged greetd.service.
if [ -f "$QD/deploy/greetd-hardening.conf" ]; then
    install -d -m 0755 /etc/systemd/system/greetd.service.d
    install -m 0644 "$QD/deploy/greetd-hardening.conf" \
        /etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf
fi
install -m 0755 "$QD/deploy/qdwin-session-launcher.sh"   /usr/local/bin/qdwin-session-launcher

# qdgreeter (boot greeter) + qdlocker (screen locker) are part of the
# production boot/session path:
#   - greetd-config.toml execs /usr/bin/qdgreeter (finding #19).
#   - qdwin-session.target Wants= qdlocker.service (finding #16).
# Earlier this config.sh assumed both were on $PATH from a "sibling
# pip-install step" that the image build never ran, so greetd booted to a
# missing greeter. Build them from the synced overlay here (build.sh now
# rsyncs qdgreeter/ + qdlocker/). --prefix=/usr lands the qdgreeter /
# qdlocker entry points in /usr/bin so greetd and the session units find
# them. --no-deps: the Python runtime deps (PyQt6, dbus_next, python-pam,
# pywayland) are RPM-installed via config.xml.
for pyapp in qdgreeter qdlocker; do
    if [ -f "$SRC/$pyapp/pyproject.toml" ]; then
        echo "[qdistro-image] pip installing $pyapp -> /usr ..."
        python3 -m pip install --break-system-packages --no-deps \
            --prefix=/usr "$SRC/$pyapp" \
            || { echo "[qdistro-image] FATAL: pip install $pyapp failed. Aborting build." >&2; exit 1; }
    else
        echo "[qdistro-image]   WARN: $SRC/$pyapp not synced — $pyapp binary will be missing"
    fi
done

# REQUIRED gate: greetd is enabled to exec /usr/bin/qdgreeter, so a
# missing greeter binary is a hard image-build failure, not a silent
# fallback to a black login (finding #19).
if [ ! -x /usr/bin/qdgreeter ]; then
    echo "[qdistro-image] FATAL: /usr/bin/qdgreeter missing after pip install;" \
         "greetd would boot to a non-existent greeter. Aborting build." >&2
    exit 1
fi
echo "[qdistro-image] /usr/bin/qdgreeter present: $(command -v qdgreeter)"
# ...and each app's QML (Main.qml + the shim module it imports) is INSIDE
# its installed package: run 28 booted to a crash-looping greeter because
# qdgreeter's wheel shipped no QML (todo/iso/14 Phase D). The gate lives in
# image/lib so the host test suite can exercise it against fake packages.
# shellcheck source=lib/pip-app-qml-gate.sh
. "$QD/image/lib/pip-app-qml-gate.sh"
pip_app_qml_gate qdgreeter qdlocker || exit 1

# Production session units. As of 2026-06-16 the VM installer above
# (install-qdwin-session-for-vm.sh) is the SINGLE SOURCE for the deploy-
# named session units — it emits qdwin-compositor.service, qdshell.service
# and qdwin-session.target directly (with the VM-specific tuning the static
# deploy/ units do NOT carry: dynamic WESTON_MODULE_MAP, conditional
# LD_LIBRARY_PATH, explicit XDG_RUNTIME_DIR=/run/user/1000). We must NOT
# re-copy deploy/qdwin-compositor.service or deploy/qdshell.service here —
# that would CLOBBER those VM-tuned units with the static vendored-path
# versions and the VM session could come up with the wrong module map or a
# missing XDG_RUNTIME_DIR. So this block only adds what the VM installer
# does NOT: the screen locker (qdlocker.service, a separate repo) and its
# wiring into qdwin-session.target.wants/.
#
# The greeter launcher does `systemctl --user start qdwin-session.target`,
# and QDWIN_SESSION_AUTOSTART=0 (exported above) keeps the installer from
# enabling the target under default.target in the image path (the greeter
# is the authoritative starter; auto-start would race for wayland-1).
ADMIN_USER_UNITS=/home/admin/.config/systemd/user
install -d -o admin -g users -m 0755 "$ADMIN_USER_UNITS"
install -d -o admin -g users -m 0755 "$ADMIN_USER_UNITS/qdwin-session.target.wants"
# qdlocker.service ships from the qdlocker repo (synced as $SRC/qdlocker).
# The upstream unit hardcodes ExecStart=/usr/local/bin/qdlocker, but the
# image pip-installs qdlocker with --prefix=/usr (above), which lands the
# console_script at /usr/bin/qdlocker. Nothing creates /usr/local/bin/qdlocker
# in the image, so copying the unit verbatim makes qdlocker.service die
# 203/EXEC at boot — the locker never starts. Render the unit through the
# SAME sed rewrite the from-source bootstrap uses so ExecStart matches the
# installed binary path (finding #16). Keep this rewrite as the authoritative
# fix even if the canonical unit is later made path-robust.
if [ -f "$SRC/qdlocker/systemd/qdlocker.service" ]; then
    sed 's|ExecStart=/usr/local/bin/qdlocker|ExecStart=/usr/bin/qdlocker|g' \
        "$SRC/qdlocker/systemd/qdlocker.service" \
        > "$ADMIN_USER_UNITS/qdlocker.service"
    chmod 0644 "$ADMIN_USER_UNITS/qdlocker.service"
    # Fail-closed: the rewritten ExecStart must point at the binary the image
    # actually installed. If the path does not resolve to an executable, the
    # unit would 203/EXEC at boot — abort the build rather than ship a locker
    # that silently never starts.
    locker_exec="$(sed -n 's|^ExecStart=\([^ ]*\).*|\1|p' \
        "$ADMIN_USER_UNITS/qdlocker.service" | head -n1)"
    if [ -z "$locker_exec" ] || [ ! -x "$locker_exec" ]; then
        echo "[qdistro-image] FATAL: qdlocker.service ExecStart ($locker_exec)" \
             "is not an executable in the image; qdlocker.service would" \
             "203/EXEC at boot. Aborting build." >&2
        exit 1
    fi
    echo "[qdistro-image] qdlocker.service ExecStart -> $locker_exec (rewritten from /usr/local/bin)"
else
    echo "[qdistro-image]   WARN: qdlocker.service not synced — locker absent from session"
fi
# Dedicated screen-unlock PAM service (harden-qdlocker 01+03). The unit's
# qdshell-path drop-in points QDLOCKER_PAM_SERVICE at `qdlocker`, so ship the
# matching /etc/pam.d/qdlocker from the synced source. It includes
# common-account for well-formed account management and enforces an explicit
# pam_faillock brute-force lockout (deny=5, unlock_time=10), decoupled from the
# borrowed `login` stack.
if [ -f "$SRC/qdlocker/pam/qdlocker" ]; then
    install -m 0644 -o root -g root "$SRC/qdlocker/pam/qdlocker" /etc/pam.d/qdlocker
    echo "[qdistro-image] /etc/pam.d/qdlocker installed (dedicated unlock PAM + faillock lockout)"
else
    echo "[qdistro-image]   WARN: qdlocker/pam/qdlocker not synced — unlock PAM service absent"
fi
# Wire qdlocker into qdwin-session.target.wants/ so the target pulls it in.
# (qdshell.service is already symlinked into qdwin-session.target.wants/ by
# the VM installer above; the locker is the piece only the image build adds,
# since qdlocker is a separate repo.) The .wants symlink is how
# `systemctl enable` would normally materialize the target's Wants=; we
# write it directly because there is no live user manager in the kiwi
# chroot. qdwin-session.target itself is NOT enabled under default.target —
# the greeter's qdwin-session-launcher starts it explicitly
# (`systemctl --user start qdwin-session.target`) after PAM auth, which is
# the authoritative session-start path (QDWIN_SESSION_AUTOSTART=0 above
# leaves the target out of default.target.wants for exactly this reason).
for unit in qdlocker.service; do
    [ -f "$ADMIN_USER_UNITS/$unit" ] || continue
    ln -sf "../$unit" "$ADMIN_USER_UNITS/qdwin-session.target.wants/$unit"
done

# Defensive: the VM installer enables qdwin-session.target under
# default.target for the headless spin-test path, but the image runuser
# shim above is meant to suppress that. Belt-and-suspenders — remove any
# default.target.wants/qdwin-session.target symlink so only the greeter-
# driven start brings up the desktop (no race for wayland-1).
rm -f "$ADMIN_USER_UNITS/default.target.wants/qdwin-session.target"
chown -R admin:users /home/admin/.config/systemd 2>/dev/null || true
echo "[qdistro-image] qdwin session: VM-installer units kept; qdlocker wired into qdwin-session.target.wants; target auto-start suppressed (greeter starts it)"

# kiwi_profiles comes from /.profile (sourced at the top). The ci kiwi
# profile is the CI golden base: greetd would grab the DRM seat and block
# admin's lingering user manager from starting the compositor (the same
# reason fresh-vm-bootstrap.sh masks greetd). Mask it; still ship the
# greeter binary so tester vs ci differs by packages + this unit, not by
# a missing greeter. tester (default) enables greetd as before.
kiwi_is_ci=0
_kp=",$(printf '%s' "${kiwi_profiles:-}" | tr ' ,' ',,'),"
case "$_kp" in
    *,ci,*) kiwi_is_ci=1 ;;
esac
if [ "$kiwi_is_ci" = 1 ]; then
    echo "[qdistro-image] kiwi profile ci: masking greetd (CI golden; admin user manager starts the compositor)"
    systemctl disable greetd.service 2>/dev/null || true
    systemctl mask greetd.service
else
    systemctl enable greetd.service
fi
# Tear down any pre-existing tty4 LXQt+labwc fallback (the passwordless escape
# hatch has been removed). Idempotent — keeps the removal correct even if an
# image build ever runs over a reused/rooted tree.
systemctl disable --now greetd-fallback.service 2>/dev/null || true
rm -f /etc/systemd/system/greetd-fallback.service /etc/greetd/config-fallback.toml
# Production recovery is via GRUB (doc/recovery.md). (The legacy tty4
# passwordless LXQt+labwc escape hatch has been removed.)
systemctl set-default graphical.target

# Keep the compositor's VT exclusively the compositor's, so seatd's K_OFF on
# it is never reverted by a getty and a locked screen cannot leak keystrokes
# into the kernel console / login(1). Scoped to the compositor VT: tty1's
# emergency agetty and tty5+ work sessions are untouched. Same helper the
# bootstrap path runs — see scripts/install/harden-compositor-vt.sh.
# REQUIRED gate: an image that ships with a getty able to take tty3 is a
# lock-security regression, so a failure aborts the build.
# --offline: this runs in the kiwi chroot, where no system manager is running
# and systemd answers runtime queries with a no-op exit 0. The helper masks
# and verifies the mask on disk, and skips only the probes that cannot be
# answered here. It is an argument rather than an environment variable so it
# cannot leak into a live install, and the helper refuses it (exit 2) if the
# root turns out to be live — so this stays a build abort, never a silent
# downgrade of the live checks.
if ! bash "$QD/scripts/install/harden-compositor-vt.sh" --offline /etc/greetd/config.toml; then
    echo "[qdistro-image] FATAL: compositor VT is not exclusively the compositor's;" \
         "a getty could take it and revert seatd's K_OFF. Aborting build." >&2
    exit 1
fi

# Keep /root/qdistro-src on the image — the LLM-modifiability principle
# in doc/overview.md requires editable Python services on disk. Drop the
# meson build dirs and __pycache__ to save ~200MB of churn.
find "$SRC" -type d \( -name build -o -name __pycache__ \) -prune -exec rm -rf {} + 2>/dev/null || true

# kiwi 10's baseCleanMount + suseConfig are deprecation stubs that exit
# non-zero; cleanup is kiwi's job now.

echo "[qdistro-image] config.sh complete."
exit 0
