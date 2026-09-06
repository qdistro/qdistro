# shellcheck shell=bash
# qdistro-offline.sh — the offline-install contract (todo/iso/14 Phase B).
#
# Sourced (never executed) by every install-*.sh in the chain. An installer
# does two kinds of work: file drops (units, policies, binaries, symlinks —
# including `systemctl enable`, which only writes wants-symlinks) and LIVE
# operations that need a running system manager, system bus or logind
# (`systemctl start`/`--now`/`daemon-reload`/`reload`, `busctl`, `loginctl`,
# user-manager calls, readiness probes). Inside the kiwi chroot the live
# operations cannot work, and worse, systemd answers several of them with a
# no-op and exit 0, so an installer that mixes them cannot tell "installed"
# from "verified". This library makes the split explicit:
#
#   . "$(dirname "$0")/lib/qdistro-offline.sh"
#   resolve_offline_install            # once, after arg parsing
#   ...file drops as normal...
#   sd_daemon_reload                   # live only
#   sd_enable_now foo.service          # enable always; start live only
#   live_only "probe foo on the bus" busctl ...   # live only
#
# Offline mode is REQUESTED by QDISTRO_OFFLINE_INSTALL=1 and only honoured
# when this root is CORROBORATED as a chroot: positive evidence
# (`systemd-detect-virt --chroot` with a real /proc/1 to compare against),
# never absence of systemd. The rule is the one harden-compositor-vt.sh
# uses; tests/integration/vm/offline-install.bats asserts the two agree.
# A leaked flag on a live machine therefore does nothing but print a
# warning: live installs stay live. The converse is deliberate: a
# corroborated chroot is offline UNCONDITIONALLY, even if a manager or bus
# happens to be reachable from inside it (a bind-mounted /run) -- the
# installers target the chroot's tree, and a reachable manager would be
# the HOST's, so starting or probing units there would be wrong anyway. Skipped operations are LOGGED one per
# line, so a build log shows exactly what the booted image still has to do
# at first boot (enable symlinks are on disk; nothing is started). The skip
# lines go to STDERR so an installer's `>/dev/null` on the command cannot eat
# them and the build log stays the complete first-boot to-do list.
#
# Residual hazard, stated plainly: the skipped operations include the
# UPGRADE mechanism (`sd_reload_dbus`, `sd_try_restart`) and the readiness
# probes. If offline were ever selected on a root that is also live (an
# operator chrooting into a running system's mount), the new code and bus
# policy would land on disk while the OLD daemon kept serving under the OLD
# policy, and nothing would have verified the fresh unit works. That is why
# offline is opt-in AND corroborated, and never auto-detected.
#
# Any other non-zero exit from an installer is a real failure; nothing here
# converts one into a warning.

QDISTRO_OFFLINE=0

# offline_root — 0 only on positive evidence that this root is a chroot.
# MIRRORS scripts/install/harden-compositor-vt.sh:offline_root; change both.
qdistro_offline_root() {
    [ -e /proc/1/comm ] || return 1
    systemd-detect-virt --chroot --quiet 2>/dev/null && return 0
    return 1
}

# resolve_offline_install — decide the mode once. Prints what it decided.
resolve_offline_install() {
    QDISTRO_OFFLINE=0
    if [ "${QDISTRO_OFFLINE_INSTALL:-0}" = 1 ]; then
        if qdistro_offline_root; then
            QDISTRO_OFFLINE=1
            printf '[offline] QDISTRO_OFFLINE_INSTALL=1 and this root is a corroborated chroot: live operations will be skipped and logged\n'
        else
            printf '[offline] WARN: QDISTRO_OFFLINE_INSTALL=1 in the environment, but this root is not corroborated as a chroot; ignoring it and running live\n' >&2
        fi
    fi
    # Deliberately NOT exported: the decision belongs to this script; a child
    # that sources the library must resolve for itself.
    return 0
}

is_offline() { [ "${QDISTRO_OFFLINE:-0}" = 1 ]; }

# live_only <label> <cmd...> — run <cmd> on a live system; offline, log the
# skip and succeed. The label is what the build log shows.
live_only() {
    local label="$1"; shift
    if is_offline; then
        printf '[offline] skipped (needs a running system manager): %s\n' "$label" >&2
        return 0
    fi
    "$@"
}

# sd_daemon_reload — live only. Callers that tolerate a live failure write
# `sd_daemon_reload || true`, never a redirect: a redirect would also hide
# the offline skip line.
sd_daemon_reload() { live_only "systemctl daemon-reload" systemctl daemon-reload; }

# sd_reload_dbus — reload the system bus's policy (dbus-broker or dbus);
# live only, best-effort as before.
sd_reload_dbus() {
    if is_offline; then
        printf '[offline] skipped (needs a running system bus): reload dbus policy\n' >&2
        return 0
    fi
    systemctl reload dbus-broker.service 2>/dev/null \
        || systemctl reload dbus.service 2>/dev/null \
        || true
}

# sd_reload_polkit — ask polkitd to re-read /usr/share/polkit-1/actions after
# a policy drop; live only, best-effort as the tier installers always were
# (polkitd enumerates actions on its next start anyway, so a failed reload
# is not an incomplete install). Offline the action file is on disk and the
# booted image's polkitd reads it at start.
sd_reload_polkit() {
    if is_offline; then
        printf '[offline] skipped (needs a running system manager): reload polkit actions\n' >&2
        return 0
    fi
    systemctl reload polkit.service 2>/dev/null \
        || pkill -HUP polkitd 2>/dev/null \
        || true
}

# sd_enable [--global] <unit...> — always: `systemctl enable` only writes
# wants-symlinks, which works in a chroot and is what the booted image obeys.
sd_enable() { systemctl enable "$@"; }

# sd_enable_now <unit...> — enable always; start live only. Replaces
# `systemctl enable --now`, whose start half systemd silently drops in a
# chroot (exit 0), leaving no record that nothing was started.
sd_enable_now() {
    systemctl enable "$@" || return $?
    live_only "systemctl start $*" systemctl start "$@"
}

# sd_start / sd_restart / sd_try_restart <unit...> — live only.
sd_start()       { live_only "systemctl start $*"       systemctl start "$@"; }
sd_restart()     { live_only "systemctl restart $*"     systemctl restart "$@"; }
sd_try_restart() { live_only "systemctl try-restart $*" systemctl try-restart "$@"; }

# linger_enable <user> — `loginctl enable-linger` needs logind; offline the
# equivalent is the on-disk marker logind reads at boot.
linger_enable() {
    local user="$1"
    if is_offline; then
        install -d -m 0755 /var/lib/systemd/linger
        : > "/var/lib/systemd/linger/$user"
        printf '[offline] loginctl enable-linger %s: wrote /var/lib/systemd/linger/%s directly\n' "$user" "$user"
        return 0
    fi
    loginctl enable-linger "$user"
}

# user_unit_enable <user> <group> <unit...> — `systemctl --user enable` for
# a user whose manager is not running (offline, or a fresh user): write the
# WantedBy=default.target symlinks under the user's default.target.wants.
# Live, the real command is used so the manager's own logic applies.
# Units that are not installed are skipped, as `systemctl enable` would
# also refuse them. Ownership follows the units dir.
user_unit_enable() {
    local user="$1" group="$2"; shift 2
    local home units target u
    home="$(getent passwd "$user" | cut -d: -f6)"
    [ -n "$home" ] || { printf 'user_unit_enable: no such user %s\n' "$user" >&2; return 1; }
    units="$home/.config/systemd/user"
    if ! is_offline; then
        runuser -l "$user" -c "systemctl --user enable $*"
        return
    fi
    target="$units/default.target.wants"
    install -d -o "$user" -g "$group" -m 0755 "$target"
    local rc=0
    for u in "$@"; do
        if [ ! -f "$units/$u" ]; then
            # `systemctl enable` fails on a unit it cannot find; so do we.
            printf '[offline] user unit %s not present under %s; cannot enable\n' "$u" "$units" >&2
            rc=1
            continue
        fi
        ln -sfn "../$u" "$target/$u"
        chown -h "$user:$group" "$target/$u"
        printf '[offline] systemctl --user enable %s for %s: wrote %s/%s\n' "$u" "$user" "$target" "$u"
    done
    return $rc
}
