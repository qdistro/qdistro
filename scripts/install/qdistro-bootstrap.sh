#!/usr/bin/env bash
# qdistro-bootstrap.sh — comprehensive, idempotent installer for qdistro.
#
# Target distros (server / minimal install):
#   - openSUSE Tumbleweed  (primary, fully supported)
#   - Ubuntu 26.04         (best-effort; some upstream packages may need
#                           fallback build-from-source — script warns and continues)
#
# What it does:
#   1.  Detects the distro.
#   2.  Prompts for admin password + a regular user (name + password).
#       The admin user is always named `admin` with uid 1000 (the rest
#       of the qdistro tree hardcodes that).
#   3.  Installs all build + runtime dependencies via the distro's
#       package manager.
#   4.  Creates admin (uid 1000) and regular user (uid 1001) accounts;
#       on btrfs systems, their home dirs become per-user subvolumes with
#       snapper configs.
#   5.  Provisions system btrfs subvolumes: /var/lib/qdistro/vaults,
#       /var/lib/libvirt/images, /var/lib/containers.
#   6.  Fetches all qdistro repos (qdistro, qdwin, qdshell, qdlocker,
#       qdbrowser, qdgreeter, qterminator, qnotebook, qfileman) — uses
#       sibling checkouts under $REPO_ROOT if present, otherwise clones
#       from Codeberg.
#   7.  Builds qdwin (libweston shell plugin) and the qdistro C daemons.
#   8.  Builds the qdshell QML plugin (libqdistro-qdwin.so).
#   9.  pip-installs qdgreeter, qdlocker, qdbrowser, qterminator,
#       qnotebook, qfileman.
#  10.  Runs the existing scripts/install/install-*.sh installer chain
#       (broker, session-manager, polkit, pwd, qsu, browser-bridge, phone,
#       print, recall, snapshots, tier3, tier5).
#  11.  Installs qdlocker systemd user service for admin.
#  12.  Installs locker configuration via deploy/install-locker-config.sh.
#  13.  (Tumbleweed) loads SELinux policy modules in permissive mode.
#  14.  Installs the qdwin user session for admin (weston + qdshell).
#  15.  Configures greetd to autologin admin into the session (tty3).
#  16.  (Opt-in) builds tier-4 guest base image.
#  17.  Runs first-boot smoke checks.
#
# Usage:
#   sudo bash qdistro-bootstrap.sh                        # interactive
#   sudo bash qdistro-bootstrap.sh \
#        --admin-password=hunter2 \
#        --user=alice --user-password=secret \
#        --yes                                            # no prompts
#
# Install profiles (the single dev-vs-hardened gate — QDISTRO_PROFILE or
# --profile):
#   dev           disposable VM / developer bring-up. Throwaway shortcuts
#                 are permitted: --no-gpg-checks fetches, env-var passwords,
#                 `admin NOPASSWD: ALL` sudoers, root pip --prefix=/usr from
#                 unpinned source checkouts, warn-and-continue on btrfs.
#   daily-driver  (DEFAULT) a real machine. Hardened: GPG-checked fetches,
#                 NO env-var passwords (use --*-password-fd or interactive),
#                 NO `NOPASSWD: ALL`, source checkouts pinned/verified before
#                 root install, pip apps go to an isolated /opt/qdistro prefix
#                 with /usr/bin wrappers, btrfs subvolume failure is FATAL.
#   release       packaged image build. Same hardening as daily-driver.
# The default is the HARDENED profile: a caller who forgets to choose still
# gets the safe path, never the throwaway one. Pass --profile=dev (or set
# QDISTRO_PROFILE=dev) explicitly for disposable VMs.
#
# CLI flags (all override the matching env var below):
#   --profile=P            dev | daily-driver | release (default daily-driver)
#   --dev                  shorthand for --profile=dev
#   --admin-password=PW    set admin's password (uid 1000). DEV ONLY — in
#                          hardened profiles a password on argv/env leaks via
#                          /proc; use --admin-password-fd or interactive.
#   --admin-password-fd=N  read admin's password from file descriptor N
#                          (hardened-safe: never appears in argv/environ).
#   --user=NAME            set the regular username (uid 1001)
#   --user-password=PW     set the regular user's password (DEV ONLY, as above)
#   --user-password-fd=N   read the regular user's password from fd N
#   --reset-passwords      re-apply the admin/regular passwords even for
#                          accounts that already exist (state-rewriting).
#                          Without this flag, a password is only set when
#                          the account is created for the first time; on a
#                          re-run, existing accounts' passwords are left
#                          untouched (and the skip is logged), so bootstrap
#                          never clobbers a password the operator changed.
#   --repo-root=DIR        directory holding all qdistro sibling checkouts
#                          (default: parent of this repo if found,
#                           else /opt/qdistro-src)
#   --branch=BR            git branch to clone (default: main)
#   --yes, -y              skip the "Proceed? [y/N]" confirmation
#   --noninteractive       fail rather than prompt for missing values
#   --skip-packages        skip the distro package install step
#   --skip-greetd          skip the autologin / boot-to-session step
#   --skip-subvolumes      skip btrfs subvolume provisioning
#   --skip-sources         skip git clone / source acquisition step
#   --skip-build           skip all build steps (qdwin, daemons, qdshell, pip).
#                          REQUIRES the build outputs to be already installed
#                          on the target (e.g. from an RPM/staged image). The
#                          runtime-required minimum (the build functions also
#                          install test helpers + protocol XML/pkg-config, not
#                          listed here) that must be present and importable
#                          (paths assume meson/pip --prefix=/usr; $libdir is
#                          /usr/lib64 on Tumbleweed, /usr/lib/<triplet> on
#                          Ubuntu):
#                            - qdwin nested-shell plugin:
#                                $libdir/weston/qdwin-shell.so
#                            - qdshell QML plugin (+ its qmldir):
#                                /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
#                                /usr/share/qdistro/qml/Qdistro/Qdwin/qmldir
#                            - compiled qdistro daemons in /usr/bin:
#                                qdistro-secctx-exec, qdistro-cursor-sprites,
#                                and (when freerdp3/winpr3/pipewire present)
#                                qdistro-forward, qdistro-nested-pixelfeed;
#                                plus $libexecdir/qdistro-tier1-exec (SELinux)
#                            - pip apps installed under /usr (modules in
#                                /usr/lib/pythonX.Y/site-packages, launchers in
#                                /usr/bin): qdgreeter, qdlocker, qdbrowser,
#                                qterminator, qnotebook, qfileman
#   --strict               make genuinely-core install failures FATAL: zypper
#                          refresh, package install, the qdwin/daemons/qdshell
#                          builds, and the installer-chain steps abort instead
#                          of warning. Optional bring-up / smoke checks stay
#                          non-fatal. Default OFF (warn-and-continue) — opt in
#                          for unattended daily-driver installs.
#   --tier4-base           opt-in: build the tier-4 guest base qcow2 image
#   -h, --help             show help and exit
#
# Equivalent env vars (CLI wins): QDISTRO_ADMIN_PASSWORD,
#   QDISTRO_USER_NAME, QDISTRO_USER_PASSWORD, QDISTRO_REPO_ROOT,
#   QDISTRO_BRANCH, QDISTRO_NONINTERACTIVE, QDISTRO_YES,
#   QDISTRO_SKIP_PACKAGES, QDISTRO_SKIP_GREETD,
#   QDISTRO_SKIP_SUBVOLUMES, QDISTRO_SKIP_SOURCES,
#   QDISTRO_SKIP_BUILD, QDISTRO_BUILD_TIER4_BASE,
#   QDISTRO_RESET_PASSWORDS, QDISTRO_STRICT.

set -euo pipefail

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
# Shared profile gate + hardened primitives (dev vs daily-driver/release).
# shellcheck source=lib/qdistro-profile.sh
. "$SCRIPT_DIR/lib/qdistro-profile.sh"
# scripts/install/<this> -> qdistro/ -> qdistro-org/
QDISTRO_DIR_DEFAULT=$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || true)
REPO_ROOT="${QDISTRO_REPO_ROOT:-${QDISTRO_DIR_DEFAULT:+$(dirname "$QDISTRO_DIR_DEFAULT")}}"
REPO_ROOT="${REPO_ROOT:-/opt/qdistro-src}"
BRANCH="${QDISTRO_BRANCH:-main}"

DISTRO=""           # "tumbleweed" | "ubuntu"
# Profile is resolved in main() via resolve_profile (defaults to the
# hardened daily-driver profile). --profile / --dev set this.
QDISTRO_PROFILE="${QDISTRO_PROFILE:-daily-driver}"
# Password file descriptors (hardened-safe input). Empty unless --*-fd given.
ADMIN_PASSWORD_FD=""
REGULAR_PASSWORD_FD=""
# Env-var passwords are read here but are ONLY honoured under the dev
# profile (enforced in prompt_inputs). In hardened profiles they are
# ignored with a warning because /proc/<pid>/environ leaks them.
ADMIN_PASSWORD="${QDISTRO_ADMIN_PASSWORD:-}"
REGULAR_USER="${QDISTRO_USER_NAME:-}"
REGULAR_PASSWORD="${QDISTRO_USER_PASSWORD:-}"
ASSUME_YES="${QDISTRO_YES:-}"
NONINTERACTIVE="${QDISTRO_NONINTERACTIVE:-}"
SKIP_PACKAGES="${QDISTRO_SKIP_PACKAGES:-}"
SKIP_GREETD="${QDISTRO_SKIP_GREETD:-}"
SKIP_SUBVOLUMES="${QDISTRO_SKIP_SUBVOLUMES:-}"
SKIP_SOURCES="${QDISTRO_SKIP_SOURCES:-}"
SKIP_BUILD="${QDISTRO_SKIP_BUILD:-}"
BUILD_TIER4_BASE="${QDISTRO_BUILD_TIER4_BASE:-}"
RESET_PASSWORDS="${QDISTRO_RESET_PASSWORDS:-}"
# Strict mode (default OFF — preserves today's behavior). When set, the
# genuinely-core install operations (zypper refresh, package install, the
# three meson/ninja build functions, and the installer-chain invocations)
# become FATAL on failure instead of merely warning, so a daily-driver
# install fails loudly rather than producing a half-installed system.
# Optional bring-up / smoke checks stay non-fatal even under strict.
STRICT="${QDISTRO_STRICT:-}"

# Set by ensure_*_account: 1 if the account already existed before this run
# (so its password must NOT be touched unless --reset-passwords is given),
# empty if the account was just created by this bootstrap run.
ADMIN_PREEXISTING=""
REGULAR_PREEXISTING=""

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

# fail_or_warn — classify a core-install operation failure. Under strict mode
# ($STRICT set, via --strict / QDISTRO_STRICT) this is FATAL (die); otherwise
# it is a non-fatal WARN, preserving the default warn-and-continue behavior.
# Use ONLY for genuinely-core operations (refresh, package install, builds,
# installer chain). Optional bring-up / smoke checks must stay on warn().
fail_or_warn() {
    if [ -n "$STRICT" ]; then
        die "$*"
    else
        warn "$*"
    fi
}

# fail_subvol — classify a btrfs per-silo subvolume failure. The subvolume is
# the snapshot + quota boundary, so a daily-driver/release install that
# cannot create it is broken and must FAIL VISIBLY. The dev profile may
# warn-and-continue (a disposable VM can live without snapshots). Independent
# of --strict (which is about package/build core ops); the subvolume boundary
# is a daily-driver/release invariant of its own.
fail_subvol() {
    if is_dev; then
        warn "$*"
    else
        die "$* — fatal in '$QDISTRO_PROFILE' profile (the per-silo subvolume is the snapshot/quota boundary; use --profile=dev to warn-and-continue)"
    fi
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (try: sudo $0)"
}

print_help() {
    # Print the leading comment banner (everything up to, but not
    # including, the first non-comment line / `set -euo pipefail`).
    awk 'NR==1{next} /^[^#]/{exit} {sub(/^# ?/,""); print}' "$SCRIPT_PATH"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --profile=*)         QDISTRO_PROFILE="${1#*=}" ;;
            --profile)           shift; QDISTRO_PROFILE="$1" ;;
            --dev)               QDISTRO_PROFILE=dev ;;
            --admin-password=*)  ADMIN_PASSWORD="${1#*=}" ;;
            --admin-password)    shift; ADMIN_PASSWORD="$1" ;;
            --admin-password-fd=*) ADMIN_PASSWORD_FD="${1#*=}" ;;
            --admin-password-fd) shift; ADMIN_PASSWORD_FD="$1" ;;
            --user=*)            REGULAR_USER="${1#*=}" ;;
            --user)              shift; REGULAR_USER="$1" ;;
            --user-password=*)   REGULAR_PASSWORD="${1#*=}" ;;
            --user-password)     shift; REGULAR_PASSWORD="$1" ;;
            --user-password-fd=*) REGULAR_PASSWORD_FD="${1#*=}" ;;
            --user-password-fd)  shift; REGULAR_PASSWORD_FD="$1" ;;
            --repo-root=*)       REPO_ROOT="${1#*=}" ;;
            --repo-root)         shift; REPO_ROOT="$1" ;;
            --branch=*)          BRANCH="${1#*=}" ;;
            --branch)            shift; BRANCH="$1" ;;
            --yes|-y)            ASSUME_YES=1 ;;
            --noninteractive)    NONINTERACTIVE=1 ;;
            --skip-packages)     SKIP_PACKAGES=1 ;;
            --skip-greetd)       SKIP_GREETD=1 ;;
            --skip-subvolumes)   SKIP_SUBVOLUMES=1 ;;
            --skip-sources)      SKIP_SOURCES=1 ;;
            --skip-build)        SKIP_BUILD=1 ;;
            --tier4-base)        BUILD_TIER4_BASE=1 ;;
            --strict)            STRICT=1 ;;
            --reset-passwords)   RESET_PASSWORDS=1 ;;
            -h|--help)           print_help; exit 0 ;;
            *) die "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done

    # Resolve + validate the profile (defaults to the hardened daily-driver).
    resolve_profile || die "invalid --profile / QDISTRO_PROFILE (want dev|daily-driver|release)"

    # Branch name is operator input that flows into a `git clone --branch`.
    # It must not be allowed to become a shell/URL interpolation surface, so
    # constrain it to a conservative git-ref-safe shape (no whitespace, no
    # shell metacharacters, no leading '-' that could be read as a flag, no
    # '..' that could escape a manifest pin). Refs like main, v1.2.3,
    # feat/foo-bar, and 40-hex SHAs all pass.
    if ! printf '%s' "$BRANCH" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._/-]*$'; then
        die "invalid --branch '$BRANCH' (git-ref-safe chars only: [A-Za-z0-9._/-], no leading dash)"
    fi
    case "$BRANCH" in
        *..*) die "invalid --branch '$BRANCH' ('..' not allowed)" ;;
    esac

    # Env-var passwords leak via /proc/<pid>/environ. Honour them ONLY under
    # the dev profile; in hardened profiles ignore them with a clear warning
    # so the operator switches to --*-password-fd or interactive entry.
    if ! qd_password_env_allowed; then
        if [ -n "${QDISTRO_ADMIN_PASSWORD:-}" ] && [ "$ADMIN_PASSWORD" = "${QDISTRO_ADMIN_PASSWORD:-}" ] && [ -z "$ADMIN_PASSWORD_FD" ]; then
            warn "ignoring QDISTRO_ADMIN_PASSWORD in '$QDISTRO_PROFILE' profile (/proc/<pid>/environ leak); use --admin-password-fd or interactive entry"
            ADMIN_PASSWORD=""
        fi
        if [ -n "${QDISTRO_USER_PASSWORD:-}" ] && [ "$REGULAR_PASSWORD" = "${QDISTRO_USER_PASSWORD:-}" ] && [ -z "$REGULAR_PASSWORD_FD" ]; then
            warn "ignoring QDISTRO_USER_PASSWORD in '$QDISTRO_PROFILE' profile; use --user-password-fd or interactive entry"
            REGULAR_PASSWORD=""
        fi
    fi

    # Fail fast: in noninteractive mode, missing required fields are an
    # immediate error before we even check root. This allows the test
    # suite to verify the error message without running as root. A password
    # supplied via fd counts as present.
    if [ -n "$NONINTERACTIVE" ]; then
        local missing=0
        [ -z "$ADMIN_PASSWORD" ]   && [ -z "$ADMIN_PASSWORD_FD" ]   && missing=1
        [ -z "$REGULAR_USER" ]     && missing=1
        [ -z "$REGULAR_PASSWORD" ] && [ -z "$REGULAR_PASSWORD_FD" ] && missing=1
        if [ "$missing" -eq 1 ]; then
            if is_dev; then
                die "noninteractive mode but missing values; supply --admin-password(-fd), --user, --user-password(-fd)"
            else
                die "noninteractive mode but missing values; supply --admin-password-fd, --user, --user-password-fd (env/argv passwords disabled in '$QDISTRO_PROFILE' profile)"
            fi
        fi
        # Validate username format early too (before root check)
        if ! printf '%s' "$REGULAR_USER" | grep -qE '^[a-z_][a-z0-9_-]*$'; then
            die "invalid username '$REGULAR_USER' (lowercase, start with letter/underscore)"
        fi
        if [ "$REGULAR_USER" = "admin" ] || [ "$REGULAR_USER" = "root" ]; then
            die "regular user must not be 'admin' or 'root'"
        fi
    fi

    # In hardened profiles, an admin/user password passed literally on argv
    # also leaks (ps/argv). Reject --admin-password / --user-password and
    # point at the fd alternative.
    if ! is_dev; then
        if [ -n "$ADMIN_PASSWORD" ] && [ -z "$ADMIN_PASSWORD_FD" ]; then
            die "--admin-password on argv is disabled in '$QDISTRO_PROFILE' profile (argv leak); use --admin-password-fd=N or interactive entry"
        fi
        if [ -n "$REGULAR_PASSWORD" ] && [ -z "$REGULAR_PASSWORD_FD" ]; then
            die "--user-password on argv is disabled in '$QDISTRO_PROFILE' profile (argv leak); use --user-password-fd=N or interactive entry"
        fi
    fi
}

# ---------------------------------------------------------------------------
# 1. Distro detection
# ---------------------------------------------------------------------------
detect_distro() {
    [ -r /etc/os-release ] || die "/etc/os-release missing — cannot identify distro"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}:${VERSION_ID:-}" in
        opensuse-tumbleweed:*)
            DISTRO=tumbleweed
            ;;
        ubuntu:26.04|ubuntu:26.*)
            DISTRO=ubuntu
            ;;
        ubuntu:*)
            warn "Ubuntu ${VERSION_ID} is not 26.04 — proceeding best-effort."
            DISTRO=ubuntu
            ;;
        *)
            die "unsupported distro: ID=${ID:-?} VERSION=${VERSION_ID:-?} (need opensuse-tumbleweed or ubuntu 26.04)"
            ;;
    esac
    log "distro: $DISTRO ($PRETTY_NAME)"
}

# ---------------------------------------------------------------------------
# 2. Prompts
# ---------------------------------------------------------------------------
prompt_password() {
    # $1 = label, sets the named var via $2
    local label="$1" varname="$2" p1="" p2=""
    while :; do
        read -r -s -p "$label: " p1 </dev/tty; echo
        read -r -s -p "$label (confirm): " p2 </dev/tty; echo
        if [ -z "$p1" ]; then
            echo "  empty password; try again." >&2
        elif [ "$p1" != "$p2" ]; then
            echo "  passwords don't match; try again." >&2
        else
            printf -v "$varname" '%s' "$p1"
            return 0
        fi
    done
}

prompt_inputs() {
    # Hardened-safe password source #1: a file descriptor. Read it FIRST so a
    # fd-supplied password counts as present and we never prompt for it. The
    # fd is closed after the read (qd_read_password_fd) so the secret source
    # does not linger open for the rest of the install.
    if [ -n "$ADMIN_PASSWORD_FD" ]; then
        qd_read_password_fd "$ADMIN_PASSWORD_FD" ADMIN_PASSWORD
        [ -n "$ADMIN_PASSWORD" ] || die "admin password fd ($ADMIN_PASSWORD_FD) was empty"
    fi
    if [ -n "$REGULAR_PASSWORD_FD" ]; then
        qd_read_password_fd "$REGULAR_PASSWORD_FD" REGULAR_PASSWORD
        [ -n "$REGULAR_PASSWORD" ] || die "user password fd ($REGULAR_PASSWORD_FD) was empty"
    fi

    # If a value is already supplied (CLI flag, fd, or dev-only env var),
    # keep it; otherwise prompt — unless --noninteractive, in which case fail.
    local need_prompt=0
    [ -z "$ADMIN_PASSWORD" ]     && need_prompt=1
    [ -z "$REGULAR_USER" ]       && need_prompt=1
    [ -z "$REGULAR_PASSWORD" ]   && need_prompt=1

    if [ -n "$NONINTERACTIVE" ] && [ "$need_prompt" -eq 1 ]; then
        die "noninteractive mode but missing values; supply --admin-password, --user, --user-password"
    fi

    if [ "$need_prompt" -eq 1 ]; then
        echo
        echo "qdistro bootstrap install — two accounts will be created:"
        echo "  1. admin   (uid 1000)  — owns hardware, approves cross-uid actions"
        echo "  2. <regular user>      — your day-to-day account"
        echo
    fi

    if [ -z "$ADMIN_PASSWORD" ]; then
        prompt_password "Password for 'admin'" ADMIN_PASSWORD
    fi
    if [ -z "$REGULAR_USER" ]; then
        local default_user="user"
        read -r -p "Regular username [$default_user]: " REGULAR_USER </dev/tty
        REGULAR_USER="${REGULAR_USER:-$default_user}"
    fi
    if ! printf '%s' "$REGULAR_USER" | grep -qE '^[a-z_][a-z0-9_-]*$'; then
        die "invalid username '$REGULAR_USER' (lowercase, start with letter/underscore)"
    fi
    if [ "$REGULAR_USER" = "admin" ] || [ "$REGULAR_USER" = "root" ]; then
        die "regular user must not be 'admin' or 'root'"
    fi
    if [ -z "$REGULAR_PASSWORD" ]; then
        prompt_password "Password for '$REGULAR_USER'" REGULAR_PASSWORD
    fi
}

# ---------------------------------------------------------------------------
# 3. Package install
# ---------------------------------------------------------------------------
install_packages_tumbleweed() {
    log "refreshing zypper repositories..."
    # GPG checking is profile-gated. dev may skip signature checks for a
    # throwaway VM with stale/unsigned local mirrors; daily-driver/release
    # MUST verify repo signatures (a tampered mirror could ship a malicious
    # root package). The flag array is empty in hardened profiles so zypper's
    # default (gpg-checks ON) applies.
    local gpg_flags=()
    if is_dev; then
        gpg_flags=( --no-gpg-checks )
        warn "dev profile: zypper --no-gpg-checks (unsigned repo metadata accepted) — NOT a release default"
    fi
    local _zypper_rc=0
    zypper -n "${gpg_flags[@]}" refresh 2>&1 \
        | sed '/^[[:space:]]*$/d' \
        | sed 's/^/  /' \
        || _zypper_rc="${PIPESTATUS[0]}"
    if [ "${_zypper_rc}" -ne 0 ]; then
        # Core op: non-fatal by default (proceed on cached metadata), but
        # fatal under --strict so unattended installs don't build on stale
        # repo data. (Under --strict this aborts; the "proceed" path only
        # applies in default mode.)
        fail_or_warn "zypper refresh failed (exit ${_zypper_rc}); default mode proceeds with cached metadata (install may fail), --strict aborts"
    fi

    # Reuse the canonical Tumbleweed list from install-deps.sh.
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/install-deps.sh"
    local extra=( git wget curl openssh tar gzip xz )
    log "installing ${#QDISTRO_PKGS[@]} build/runtime packages + ${#extra[@]} extras..."
    # Core op: already fatal by default via `set -e`. Guarded explicitly so the
    # failure carries a clear message in both modes (behavior unchanged: fatal).
    zypper -n install --no-recommends "${QDISTRO_PKGS[@]}" "${extra[@]}" \
        || die "package install failed (zypper -n install); cannot continue"

    # qdbrowser WebEngine — best-effort; pdf/video degrades without it.
    # Try both known package names and warn (not die) on failure.
    local webengine_pkgs=( python313-qt6-webengine python313-PyQt6-WebEngine )
    local found_webengine=0
    for wpkg in "${webengine_pkgs[@]}"; do
        if zypper -n install --no-recommends "$wpkg" >/dev/null 2>&1; then
            log "  qdbrowser WebEngine: installed via $wpkg"
            found_webengine=1
            break
        fi
    done
    if [ "$found_webengine" -eq 0 ]; then
        warn "  qdbrowser WebEngine package not found (tried: ${webengine_pkgs[*]})"
        warn "  qdbrowser will work but pdf/video rendering will degrade."
        warn "  Check: zypper search qt6 webengine python"
    fi
}

install_packages_ubuntu() {
    log "updating apt..."
    export DEBIAN_FRONTEND=noninteractive
    # Core op: already fatal by default via `set -e`; explicit message keeps
    # behavior unchanged (fatal) while making the failure legible.
    apt-get update -qq || die "apt-get update failed; cannot continue"

    # Ubuntu equivalents of QDISTRO_PKGS. Ubuntu ships single -dev
    # packages rather than versioned runtime libs, so this list is
    # shorter. Anything missing in 26.04 will be reported and the
    # install continues so the user can build it from source after.
    local pkgs=(
        # compositor + graphics
        weston libweston-14-dev
        libfreerdp3-dev libfreerdp-server3-dev libwinpr3-dev freerdp3-x11
        libpixman-1-dev
        libwayland-dev wayland-protocols libxkbcommon-dev libevdev-dev
        libinput-dev libgbm-dev libdrm-dev libseat-dev
        libxcursor-dev adwaita-icon-theme-full
        # audio / media
        pipewire wireplumber pipewire-bin libpipewire-0.3-dev
        gstreamer1.0-pipewire gstreamer1.0-plugins-good gstreamer1.0-tools
        # build tools
        meson ninja-build build-essential pkg-config
        # python stack
        python3 python3-pip python3-setuptools python3-cffi
        python3-pyqt6 python3-dbus python3-gi python3-yaml python3-cryptography
        python3-pam python3-pywayland
        qt6-wayland-dev
        qml6-module-qtqml-workerscript
        # mesa
        libegl1 libgl1 mesa-utils libglx-mesa0
        # wayland utils
        wayland-utils
        # auth / security
        fprintd tpm2-tools libselinux1-dev
        auditd
        # sandbox / containers
        podman slirp4netns fuse-overlayfs crun
        # session bits
        waypipe wl-clipboard libnotify-bin
        greetd
        # virtualization
        libvirt-daemon-system libvirt-clients virtinst
        qemu-system-x86 qemu-utils
        libguestfs-tools
        # storage
        sqlite3 socat
        # btrfs / snapshots
        btrfs-progs snapper
        # quickshell — typically not in main archive yet; warn if absent
        quickshell
        # python3-dbus-next for qdlocker
        python3-dbus-next
        # extras
        git wget curl openssh-client tar gzip xz-utils
    )

    log "installing ${#pkgs[@]} packages (best-effort)..."
    local missing=()
    local available=()
    for p in "${pkgs[@]}"; do
        if apt-cache show "$p" >/dev/null 2>&1; then
            available+=("$p")
        else
            missing+=("$p")
        fi
    done

    # Core op: already fatal by default via `set -e`; explicit message keeps
    # behavior unchanged (fatal).
    apt-get install -y --no-install-recommends "${available[@]}" \
        || die "package install failed (apt-get install); cannot continue"

    if [ ${#missing[@]} -gt 0 ]; then
        warn "the following packages were NOT found in apt and were skipped:"
        for p in "${missing[@]}"; do warn "  - $p"; done
        warn "you may need to build these from source or enable a PPA."
        warn "common culprits on 26.04: quickshell, libweston-14-dev (if upstream is on 13)."
    fi
}

install_packages() {
    if [ -n "$SKIP_PACKAGES" ]; then
        log "skipping package install (--skip-packages)"
        return 0
    fi
    case "$DISTRO" in
        tumbleweed) install_packages_tumbleweed ;;
        ubuntu)     install_packages_ubuntu ;;
    esac
}

# ---------------------------------------------------------------------------
# 4. Btrfs detection + subvolume helpers
# ---------------------------------------------------------------------------
detect_btrfs() {
    if [ -n "$SKIP_SUBVOLUMES" ]; then return 0; fi
    local fstype
    fstype=$(findmnt -o FSTYPE / --noheadings 2>/dev/null | tr -d ' ' || echo unknown)
    if [ "$fstype" != "btrfs" ]; then
        warn "root filesystem is '$fstype', not btrfs — subvolume layout and snapshots disabled"
        SKIP_SUBVOLUMES=1
    fi
}

provision_subvolumes() {
    if [ -n "$SKIP_SUBVOLUMES" ]; then
        log "skipping subvolume provisioning (--skip-subvolumes)"
        return 0
    fi
    log "provisioning btrfs subvolumes..."
    for path in /var/lib/qdistro/vaults /var/lib/libvirt/images /var/lib/containers; do
        install -d -m 0755 "$(dirname "$path")"
        if btrfs subvolume show "$path" >/dev/null 2>&1; then
            log "  $path: already a subvolume"
        elif [ -d "$path" ] && [ "$(ls -A "$path" 2>/dev/null)" ]; then
            # The subvolume is the snapshot + quota boundary. In hardened
            # profiles a path that cannot be made a subvolume (because a
            # non-empty plain dir already occupies it) is a FATAL config
            # error, not a silent downgrade; dev keeps warn-and-continue.
            fail_subvol "  $path: directory exists and is non-empty, not converting to subvolume"
        else
            [ -d "$path" ] && rmdir "$path" 2>/dev/null || true
            # btrfs subvolume create is already fatal under set -e in every
            # profile; the snapshot/quota boundary must exist.
            btrfs subvolume create "$path"
            log "  $path: created"
        fi
    done

    # Snapper config for vaults subvolume.
    #
    # Config name MUST match the snapshots daemon's default
    # (snapshots/qdistro_snapshots.py vault_snapshot(config="qdistro_vaults"))
    # so the retention we set here actually governs the per-change vault
    # snapshots the daemon creates. The daemon uses CreateSingleSnapshot with
    # the "number" cleanup algorithm (not timeline), so the documented vault
    # retention (doc/filesystem.md "Retention policy": snapshot every change,
    # keep ~90 days) maps onto NUMBER cleanup, not the timeline knobs:
    #   - NUMBER_CLEANUP=yes  enable number-algorithm cleanup
    #   - NUMBER_MIN_AGE=7776000  protect anything younger than 90 days
    #       (90*24*3600 s) from cleanup — establishes the 90-day FLOOR
    #   - NUMBER_LIMIT=10000  high absolute-count cap.
    #       NOTE: Snapper number-cleanup has no pure time-based deletion; it
    #       only deletes once the count exceeds NUMBER_LIMIT (and never below
    #       NUMBER_MIN_AGE). So this guarantees ">= 90 days retained" exactly
    #       (the documented intent) and caps unbounded growth; snapshots older
    #       than 90 days may linger until the 10000 cap is hit. For a small,
    #       cheap, security-sensitive vault, erring toward retention is the
    #       safe default.
    #   - TIMELINE_CREATE=no  the daemon snapshots per-change, so we do not
    #       also want Snapper's timer creating timeline snapshots here
    if command -v snapper >/dev/null 2>&1; then
        if ! snapper list-configs 2>/dev/null | grep -qw "qdistro_vaults"; then
            if snapper -c qdistro_vaults create-config /var/lib/qdistro/vaults 2>/dev/null; then
                # Non-fatal like create-config above: only runs inside this
                # just-created branch so an operator's hand-tuned existing
                # config is never clobbered.
                snapper -c qdistro_vaults set-config \
                    NUMBER_CLEANUP=yes NUMBER_MIN_AGE=7776000 NUMBER_LIMIT=10000 \
                    TIMELINE_CREATE=no TIMELINE_CLEANUP=yes \
                    TIMELINE_LIMIT_YEARLY=0 2>/dev/null \
                    || warn "  snapper vault set-config (retention) failed (non-fatal)"
            else
                warn "  snapper vault config failed (non-fatal)"
            fi
        fi
    fi
}

create_user_subvolume() {
    # $1 = username, $2 = uid
    local user="$1" uid="$2"
    local home="/home/$user"
    if [ -n "$SKIP_SUBVOLUMES" ]; then
        install -d -m 0700 "$home"
        chown "${uid}:${uid}" "$home"
        return 0
    fi
    if btrfs subvolume show "$home" >/dev/null 2>&1; then
        log "  /home/$user: already a subvolume"
    elif [ -d "$home" ] && [ "$(ls -A "$home" 2>/dev/null)" ]; then
        # Per-silo home subvolume = snapshot + quota boundary. Hardened
        # profiles FAIL here; dev warns and continues.
        fail_subvol "  /home/$user: directory exists and is non-empty, not converting to subvolume"
    else
        [ -d "$home" ] && rmdir "$home" 2>/dev/null || true
        btrfs subvolume create "$home"
        chown "${uid}:${uid}" "$home"
        chmod 700 "$home"
        log "  /home/$user: subvolume created"
    fi
    # Snapper config for per-user home
    if command -v snapper >/dev/null 2>&1; then
        if ! snapper list-configs 2>/dev/null | grep -q "home-${user}"; then
            if snapper -c "home-${user}" create-config "$home" 2>/dev/null; then
                # Apply the documented per-user-home retention
                # (doc/filesystem.md "Retention policy"): hourly 24, daily 30,
                # weekly 12, monthly 6, yearly 0. Enable timeline create+cleanup
                # so the limits are actually enforced. Non-fatal like
                # create-config above; only runs inside this just-created branch
                # so an operator's hand-tuned existing config is never clobbered.
                snapper -c "home-${user}" set-config \
                    TIMELINE_CREATE=yes TIMELINE_CLEANUP=yes \
                    TIMELINE_LIMIT_HOURLY=24 TIMELINE_LIMIT_DAILY=30 \
                    TIMELINE_LIMIT_WEEKLY=12 TIMELINE_LIMIT_MONTHLY=6 \
                    TIMELINE_LIMIT_YEARLY=0 2>/dev/null \
                    || warn "  snapper set-config (retention) for $user failed (non-fatal)"
            else
                warn "  snapper config for $user failed (non-fatal)"
            fi
        fi
    fi
}

# ---------------------------------------------------------------------------
# 5. Users
# ---------------------------------------------------------------------------
user_for_uid() {
    local uid="$1"
    getent passwd | awk -F: -v uid="$uid" '$3 == uid { print $1; exit }'
}

ensure_admin_account() {
    local existing
    ADMIN_PREEXISTING=""

    if getent passwd admin >/dev/null; then
        if [ "$(id -u admin)" != "1000" ]; then
            die "user 'admin' exists but is not uid 1000 (found uid $(id -u admin))"
        fi
        ADMIN_PREEXISTING=1
        return 0
    fi

    existing="$(user_for_uid 1000 || true)"
    if [ -n "$existing" ]; then
        die "uid 1000 already belongs to '$existing'; qdistro requires an 'admin' user at uid 1000"
    fi

    # Do not use -m; we handle home creation via create_user_subvolume.
    useradd -M -u 1000 -U -s /bin/bash admin
}

ensure_regular_account() {
    local existing
    REGULAR_PREEXISTING=""

    if getent passwd "$REGULAR_USER" >/dev/null; then
        if [ "$(id -u "$REGULAR_USER")" != "1001" ]; then
            die "user '$REGULAR_USER' exists but is not uid 1001 (found uid $(id -u "$REGULAR_USER"))"
        fi
        REGULAR_PREEXISTING=1
        return 0
    fi

    existing="$(user_for_uid 1001 || true)"
    if [ -n "$existing" ]; then
        die "uid 1001 already belongs to '$existing'; choose a fresh qdistro user or free uid 1001"
    fi

    # Do not use -m; we handle home creation via create_user_subvolume.
    useradd -M -u 1001 -U -s /bin/bash "$REGULAR_USER"
}

create_users() {
    log "creating admin user (uid 1000)..."
    ensure_admin_account
    create_user_subvolume admin 1000
    # Setting a password is state-rewriting: only do it when the account was
    # just created, or when --reset-passwords is given. This keeps re-runs
    # idempotent and avoids clobbering a password the operator changed.
    if [ -z "$ADMIN_PREEXISTING" ] || [ -n "$RESET_PASSWORDS" ]; then
        echo "admin:${ADMIN_PASSWORD}" | chpasswd
    else
        log "  admin already exists; leaving its password unchanged (use --reset-passwords to override)"
    fi
    # wheel group on Tumbleweed, sudo on Ubuntu
    if getent group wheel >/dev/null; then usermod -aG wheel admin; fi
    if getent group sudo  >/dev/null; then usermod -aG sudo  admin; fi
    # Sudoers policy is profile-gated:
    #   dev           — passwordless `admin ALL=(ALL) NOPASSWD: ALL` so a
    #                   throwaway VM is frictionless. This is the named
    #                   dev-only escape hatch.
    #   daily-driver/ — NO passwordless-everything rule. admin keeps normal
    #   release         wheel/sudo membership (password-REQUIRED sudo); all
    #                   cross-uid privileged actions flow through qsu / the
    #                   broker's scoped approval, which is the qdistro design.
    # Any stale 99-admin NOPASSWD file from a previous dev install is removed
    # in hardened profiles so a re-run upgrades the box rather than leaving
    # the passwordless rule behind.
    if is_dev; then
        install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'
        warn "dev profile: installed passwordless sudoers (admin NOPASSWD: ALL) — NOT a release default"
    else
        if [ -e /etc/sudoers.d/99-admin ]; then
            rm -f /etc/sudoers.d/99-admin
            log "  removed stale passwordless sudoers /etc/sudoers.d/99-admin (hardened profile)"
        fi
        log "  hardened profile: admin uses password-required sudo; cross-uid actions go through qsu/broker"
    fi

    log "creating regular user '$REGULAR_USER' (uid 1001)..."
    ensure_regular_account
    create_user_subvolume "$REGULAR_USER" 1001
    if [ -z "$REGULAR_PREEXISTING" ] || [ -n "$RESET_PASSWORDS" ]; then
        echo "${REGULAR_USER}:${REGULAR_PASSWORD}" | chpasswd
    else
        log "  $REGULAR_USER already exists; leaving its password unchanged (use --reset-passwords to override)"
    fi
    # regular user gets no sudo; cross-uid actions go through the broker.

    # Both users need video/input/render for any compositor session.
    for g in video input render; do
        getent group "$g" >/dev/null || continue
        usermod -aG "$g" admin
        usermod -aG "$g" "$REGULAR_USER"
    done
}

# ---------------------------------------------------------------------------
# 6. Source acquisition
# ---------------------------------------------------------------------------
repo_url() {
    case "$1" in
        qnotebook)  echo "https://codeberg.org/qnotebook/qnotebook.git" ;;
        qterminator) echo "https://codeberg.org/qterminator/qterminator.git" ;;
        *)          echo "https://codeberg.org/qdistro/$1.git" ;;
    esac
}

repo_present() {
    local repo="$1"
    [ -d "$REPO_ROOT/$repo/.git" ] && return 0
    case "$repo" in
        qdistro) [ -d "$REPO_ROOT/$repo/daemons" ] ;;
        qdwin|qdshell) [ -f "$REPO_ROOT/$repo/meson.build" ] ;;
        *) [ -f "$REPO_ROOT/$repo/pyproject.toml" ] ;;
    esac
}

# Source manifest: maps each repo to a pinned 40-hex commit SHA. Default
# location is scripts/install/source-manifest.txt, overridable via
# QDISTRO_SOURCE_MANIFEST. In hardened (daily-driver/release) profiles this
# manifest is REQUIRED before any root build/install from a checkout: we will
# not run `meson install` / root `pip install` from an unpinned, unverified
# tree. dev installs may skip it (shallow branch clones).
SOURCE_MANIFEST="${QDISTRO_SOURCE_MANIFEST:-$SCRIPT_DIR/source-manifest.txt}"

# manifest_pin <repo> — print the pinned 40-hex SHA for <repo>, or empty if
# the repo is absent / the manifest does not exist. Lines: `repo <sha>`,
# '#' comments and blanks ignored.
manifest_pin() {
    local repo="$1"
    [ -f "$SOURCE_MANIFEST" ] || return 0
    awk -v r="$repo" '
        /^[[:space:]]*#/ {next}
        $1==r { print $2; exit }
    ' "$SOURCE_MANIFEST"
}

# verify_repo_pin <repo> — in hardened profiles, ensure the checkout at
# $REPO_ROOT/<repo> is exactly at its manifest-pinned commit; check it out if
# a fetched full clone allows. FATAL on any mismatch / missing pin / detached
# unpinned state. No-op (returns 0) under dev.
verify_repo_pin() {
    local repo="$1" pin head
    is_dev && return 0
    pin="$(manifest_pin "$repo")"
    if [ -z "$pin" ]; then
        die "$repo: no manifest pin in $SOURCE_MANIFEST; hardened profiles refuse to root-install from an unpinned source tree (add a '<repo> <40-hex-sha>' line, or --profile=dev)"
    fi
    if ! printf '%s' "$pin" | grep -qE '^[0-9a-f]{40}$'; then
        die "$repo: manifest pin '$pin' is not a 40-hex commit SHA"
    fi
    [ -d "$REPO_ROOT/$repo/.git" ] || die "$repo: pinned profile needs a git checkout to verify $pin (found a non-git tree at $REPO_ROOT/$repo)"
    # Make sure the pinned object exists, then check it out.
    if ! git -C "$REPO_ROOT/$repo" cat-file -e "${pin}^{commit}" 2>/dev/null; then
        git -C "$REPO_ROOT/$repo" fetch --quiet origin "$pin" 2>/dev/null || true
    fi
    git -C "$REPO_ROOT/$repo" cat-file -e "${pin}^{commit}" 2>/dev/null \
        || die "$repo: pinned commit $pin not present in checkout (shallow clone? fetch the pin or supply a full checkout)"
    git -C "$REPO_ROOT/$repo" checkout --quiet --detach "$pin" \
        || die "$repo: failed to check out pinned commit $pin"
    head="$(git -C "$REPO_ROOT/$repo" rev-parse HEAD 2>/dev/null || true)"
    [ "$head" = "$pin" ] || die "$repo: HEAD ($head) != pinned $pin after checkout"
    log "  $repo: verified at pinned commit $pin"
}

fetch_repo() {
    local repo="$1"
    local fatal="${2:-fatal}"
    local url

    if repo_present "$repo"; then
        log "  $repo: using existing checkout at $REPO_ROOT/$repo"
        verify_repo_pin "$repo"
        return 0
    fi

    url="$(repo_url "$repo")"
    # Pinned (hardened) profiles need the pinned commit object available, so
    # a shallow --branch clone is not enough — do a full clone and let
    # verify_repo_pin detach to the manifest SHA. dev keeps the fast shallow
    # branch clone (branch already validated git-ref-safe in parse_args).
    if is_dev; then
        log "  $repo: cloning $url (branch=$BRANCH, shallow — dev)..."
        if git clone --depth 1 --branch "$BRANCH" "$url" "$REPO_ROOT/$repo"; then
            return 0
        fi
    else
        log "  $repo: cloning $url (full, will pin per manifest)..."
        if git clone "$url" "$REPO_ROOT/$repo"; then
            verify_repo_pin "$repo"
            return 0
        fi
    fi

    if [ "$fatal" = "fatal" ]; then
        die "$repo: clone failed"
    fi
    warn "  $repo: clone failed (non-fatal; install will be skipped)"
}

fetch_sources() {
    if [ -n "$SKIP_SOURCES" ]; then
        log "skipping source acquisition (--skip-sources)"
        return 0
    fi
    install -d -m 0755 "$REPO_ROOT"
    cd "$REPO_ROOT"

    for repo in qdistro qdwin qdshell; do
        fetch_repo "$repo" fatal
    done

    for repo in qdlocker qdbrowser qdgreeter qterminator qnotebook qfileman; do
        fetch_repo "$repo" optional
    done

    [ -f "$REPO_ROOT/qdwin/meson.build" ]   || die "qdwin tree missing meson.build"
    [ -d "$REPO_ROOT/qdistro/daemons" ]     || die "qdistro tree missing daemons/"
}

# ---------------------------------------------------------------------------
# 7. Build qdwin + qdistro daemons
# ---------------------------------------------------------------------------
#
# --skip-build contract: the four build_* / pip steps below are skipped when
# SKIP_BUILD is set, so the artifacts they would normally `meson install` /
# `pip install` MUST already be present on the target (staged image / RPM).
# Derived from the meson.build / pyproject.toml of each repo; paths assume
# --prefix=/usr ($libdir = /usr/lib64 on Tumbleweed, /usr/lib/<triplet> on
# Ubuntu):
#   build_qdwin           -> $libdir/weston/qdwin-shell.so
#                            (shared_library 'qdwin-shell', name_prefix '',
#                             install_dir libdir/weston — the nested weston
#                             shell plugin weston dlopens via --shell)
#   build_qdistro_daemons -> /usr/bin/qdistro-secctx-exec
#                            /usr/bin/qdistro-cursor-sprites
#                            /usr/bin/qdistro-forward         (if freerdp3/winpr3/pipewire)
#                            /usr/bin/qdistro-nested-pixelfeed(if libpipewire-0.3)
#                            $libexecdir/qdistro-tier1-exec   (if libselinux)
#   build_qdshell_plugin  -> /usr/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
#                            /usr/share/qdistro/qml/Qdistro/Qdwin/qmldir
#                            (QML plugin; reached via QML_IMPORT_PATH=/usr/share/qdistro/qml)
#   pip_install_apps      -> /usr/bin/<app> launchers + Python modules under
#                            /usr/lib/pythonX.Y/site-packages for:
#                            qdgreeter qdlocker qdbrowser qterminator qnotebook qfileman
# If any of the above is absent on a --skip-build run, the corresponding
# session/app component will fail at runtime (e.g. weston cannot load
# qdwin-shell.so → no compositor).
build_qdwin() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping qdwin build (--skip-build)"
        return 0
    fi
    log "building qdwin (libweston shell plugin)..."
    cd "$REPO_ROOT/qdwin"
    # Core build: already fatal by default via `set -e`; explicit messages
    # mirror build_qdshell_plugin and keep behavior unchanged (fatal).
    if [ -f build/build.ninja ]; then
        meson setup build --reconfigure --prefix=/usr \
            || die "qdwin meson setup failed"
    else
        meson setup build --prefix=/usr \
            || die "qdwin meson setup failed"
    fi
    meson compile -C build || die "qdwin meson compile failed"
    meson install -C build || die "qdwin meson install failed"
}

build_qdistro_daemons() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping qdistro daemons build (--skip-build)"
        return 0
    fi
    log "building qdistro C daemons..."
    cd "$REPO_ROOT/qdistro/daemons"
    # Core build: already fatal by default via `set -e`; explicit messages
    # keep behavior unchanged (fatal).
    if [ -f build/build.ninja ]; then
        meson setup build --reconfigure --prefix=/usr \
            || die "qdistro daemons meson setup failed"
    else
        meson setup build --prefix=/usr \
            || die "qdistro daemons meson setup failed"
    fi
    meson compile -C build || die "qdistro daemons meson compile failed"
    meson install -C build || die "qdistro daemons meson install failed"
}

build_qdshell_plugin() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping qdshell QML plugin build (--skip-build)"
        return 0
    fi
    log "building qdshell QML plugin (libqdistro-qdwin.so)..."
    cd "$REPO_ROOT/qdshell"
    if [ -f build/build.ninja ]; then
        meson setup build --reconfigure --prefix=/usr \
            || die "qdshell meson setup failed"
    else
        meson setup build --prefix=/usr \
            || die "qdshell meson setup failed"
    fi
    meson compile -C build \
        || die "qdshell meson compile failed"
    meson install -C build \
        || die "qdshell meson install failed"
}

# Isolated install prefix for source-built Python apps in hardened profiles.
# Writing app files into RPM-owned /usr widens upgrade/audit collision risk,
# so daily-driver/release install into /opt/qdistro and expose launchers via
# small /usr/bin wrappers. dev keeps the simpler --prefix=/usr.
QDISTRO_OPT_PREFIX="${QDISTRO_OPT_PREFIX:-/opt/qdistro}"

# pip_install_one <app> — install one source-built Python app under the
# profile-appropriate prefix. In hardened profiles the source must already be
# manifest-pinned + verified (verify_repo_pin ran during fetch_sources).
pip_install_one() {
    local app="$1" launcher
    if is_dev; then
        # dev: write into /usr (collides with RPM ownership, but a throwaway
        # VM does not care). --prefix=/usr launchers land in /usr/bin.
        python3 -m pip install --break-system-packages --no-deps --prefix=/usr --quiet \
            "$REPO_ROOT/$app" \
            || warn "  pip install $app failed (non-fatal)"
        return 0
    fi
    # Hardened: isolated /opt/qdistro prefix; no writes into RPM-owned /usr.
    install -d -m 0755 "$QDISTRO_OPT_PREFIX"
    python3 -m pip install --no-deps --prefix="$QDISTRO_OPT_PREFIX" --quiet \
        "$REPO_ROOT/$app" \
        || { warn "  pip install $app -> $QDISTRO_OPT_PREFIX failed (non-fatal)"; return 0; }
    # Expose the launcher on PATH via a thin wrapper that sets PYTHONPATH to
    # the isolated prefix's site dir. Avoids touching /usr/lib*/python*/site.
    launcher="$QDISTRO_OPT_PREFIX/bin/$app"
    if [ -x "$launcher" ]; then
        cat > "/usr/bin/$app" <<EOF
#!/bin/sh
# qdistro wrapper -> isolated $QDISTRO_OPT_PREFIX install (hardened profile).
PYBASE="$QDISTRO_OPT_PREFIX/lib"
for d in "\$PYBASE"/python*/site-packages; do
    [ -d "\$d" ] && PYTHONPATH="\${PYTHONPATH:+\$PYTHONPATH:}\$d"
done
export PYTHONPATH PYTHONNOUSERSITE=1
exec "$launcher" "\$@"
EOF
        chmod 0755 "/usr/bin/$app"
        log "  $app: installed to $QDISTRO_OPT_PREFIX + /usr/bin/$app wrapper"
    else
        warn "  $app: no launcher at $launcher after pip install"
    fi
}

pip_install_apps() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping pip install of apps (--skip-build)"
        return 0
    fi
    if is_dev; then
        log "installing Python apps via pip --prefix=/usr (dev profile)..."
    else
        log "installing Python apps to isolated $QDISTRO_OPT_PREFIX (+ /usr/bin wrappers)..."
    fi
    for app in qdgreeter qdlocker qdbrowser qterminator qnotebook qfileman; do
        if [ -f "$REPO_ROOT/$app/pyproject.toml" ]; then
            log "  pip install $app..."
            pip_install_one "$app"
        else
            warn "  $app source not found at $REPO_ROOT/$app"
        fi
    done

    # Because the installs above use --no-deps, a missing *runtime* dependency
    # is not caught here — it only surfaces when the app first launches. Run a
    # lightweight import smoke check per app so a broken dependency closure is
    # flagged loudly now instead of at first use. Maps each pip package to its
    # importable top-level module (verified against each repo's package dir /
    # pyproject [tool.setuptools.packages.find]). Non-fatal: WARN, same as the
    # install step above.
    log "  import smoke check (qdgreeter, qdlocker, qdbrowser)..."
    # In hardened profiles the modules live under the isolated prefix, so
    # point PYTHONPATH at it for the smoke import (matches the /usr/bin
    # wrapper); dev installs into /usr so the default path resolves.
    local smoke smoke_pp=""
    if ! is_dev; then
        local d
        for d in "$QDISTRO_OPT_PREFIX"/lib/python*/site-packages; do
            [ -d "$d" ] && smoke_pp="${smoke_pp:+$smoke_pp:}$d"
        done
    fi
    for smoke in qdgreeter qdlocker qdbrowser; do
        if [ -f "$REPO_ROOT/$smoke/pyproject.toml" ]; then
            PYTHONPATH="$smoke_pp" PYTHONNOUSERSITE=1 python3 -c "import $smoke" \
                || warn "  import smoke check failed: \"import $smoke\" — $smoke installed but a runtime dependency is missing (--no-deps skips dep resolution); the app may fail to launch"
        fi
    done
}

# ---------------------------------------------------------------------------
# 8. Python modules + systemd units (chain the existing installers)
# ---------------------------------------------------------------------------
install_python_modules() {
    log "installing Python modules + systemd units..."
    local QD="$REPO_ROOT/qdistro"
    local installers=(
        "scripts/install/install-broker-for-qdwin.sh       $QD/broker"
        "scripts/install/install-session-manager.sh        $QD/session_manager"
        "scripts/install/install-polkit-agent-for-vm.sh    $QD/polkit"
        "scripts/install/install-pwd-for-vm.sh             $QD/pwd"
        "scripts/install/install-qsu-for-vm.sh             $QD/qsu"
        "scripts/install/install-browser-bridge-for-vm.sh  $QD/browser_bridge"
        "scripts/install/install-portal-backend-for-vm.sh  $QD"
        "scripts/install/install-phone-for-vm.sh           $QD/phone"
        "scripts/install/install-print-proxy-for-vm.sh     $QD/print"
        "scripts/install/install-recall-for-vm.sh          $QD"
        "scripts/install/install-snapshots-for-vm.sh       $QD/snapshots"
        "scripts/install/install-tier3-for-vm.sh           $QD"
        "scripts/install/install-tier4-host-for-vm.sh      $QD"
        "scripts/install/install-tier5-for-vm.sh           $QD"
        "scripts/install/install-tier5b-for-vm.sh          $QD"
    )
    cd "$QD"
    for entry in "${installers[@]}"; do
        # shellcheck disable=SC2086
        set -- $entry
        local installer="$1" src_dir="$2"
        if [ -x "$installer" ]; then
            log "  -> $(basename "$installer")"
            # Core op: installer-chain step. Non-fatal by default
            # (warn-and-continue), fatal under --strict.
            bash "$installer" "$src_dir" \
                || fail_or_warn "$installer failed (default mode continues, --strict aborts)"
        else
            # A missing/unexecutable core installer is also a core failure.
            fail_or_warn "  installer not found or not executable: $installer"
        fi
    done
}

# ---------------------------------------------------------------------------
# 9. qdlocker systemd user service
# ---------------------------------------------------------------------------
install_qdlocker_service() {
    local locker_src="$REPO_ROOT/qdlocker"
    [ -d "$locker_src/systemd" ] || { warn "qdlocker systemd dir not found"; return 0; }

    # Build the final unit in a ROOT-OWNED staging file, then install it
    # atomically into admin's unit dir. We do NOT sed -i an already-installed
    # unit (a post-install in-place edit on a user-controlled tree is a
    # symlink/TOCTOU hazard), and we do NOT recursively chown the whole
    # ~/.config/systemd tree (a recursive chown follows symlinks the user
    # could plant). The qdlocker launcher path is profile-dependent:
    #   dev           -> /usr/bin/qdlocker (pip --prefix=/usr)
    #   daily/release -> /usr/bin/qdlocker wrapper into /opt/qdistro
    # In both cases the canonical launcher is /usr/bin/qdlocker, so rewrite
    # the upstream /usr/local/bin ExecStart to /usr/bin in the staging copy.
    local stage admin_grp unit_dir="/home/admin/.config/systemd/user"
    admin_grp="$(id -gn admin)"
    stage="$(mktemp)" || { warn "qdlocker: mktemp staging failed"; return 0; }
    # Render the unit with the corrected ExecStart into the root-owned stage.
    sed 's|ExecStart=/usr/local/bin/qdlocker|ExecStart=/usr/bin/qdlocker|g' \
        "$locker_src/systemd/qdlocker.service" > "$stage"
    # Atomic, ownership-explicit install (no -R, no symlink following).
    install -d -m 0755 -o admin -g "$admin_grp" /home/admin/.config/systemd
    install -d -m 0755 -o admin -g "$admin_grp" "$unit_dir"
    install -m 0644 -o admin -g "$admin_grp" "$stage" "$unit_dir/qdlocker.service"
    rm -f "$stage"
    log "  qdlocker.service staged (root-owned) + installed atomically (ExecStart -> /usr/bin/qdlocker)"

    install -d -m 0755 /etc/systemd/user/qdlocker.service.d
    cat > /etc/systemd/user/qdlocker.service.d/qdshell-path.conf <<'EOF'
[Service]
Environment=QDLOCKER_QDSHELL_PATH=/usr/share/quickshell/qdshell
Environment=QDLOCKER_PAM_SERVICE=login
EOF
    runuser -l admin -c 'systemctl --user daemon-reload' 2>/dev/null || true
    if runuser -l admin -c 'systemctl --user enable qdlocker.service' 2>/dev/null; then
        log "qdlocker service installed and enabled for admin"
    else
        warn "qdlocker service installed but enable failed (loginctl enable-linger may be needed)"
    fi
}

# ---------------------------------------------------------------------------
# 10. Locker config
# ---------------------------------------------------------------------------
install_locker_config_file() {
    local cfg_installer="$REPO_ROOT/qdistro/deploy/install-locker-config.sh"
    if [ -x "$cfg_installer" ]; then
        log "installing locker config..."
        bash "$cfg_installer" || warn "locker config install failed"
    fi
}

# ---------------------------------------------------------------------------
# 11. SELinux policy (Tumbleweed only — Ubuntu uses AppArmor)
# ---------------------------------------------------------------------------
install_selinux_policies() {
    if [ "$DISTRO" != tumbleweed ]; then
        warn "skipping SELinux policy modules — Ubuntu uses AppArmor (qdistro AppArmor policy is not yet packaged)"
        return 0
    fi
    if ! command -v semodule >/dev/null 2>&1; then
        warn "semodule not found; skipping SELinux policy install"
        return 0
    fi
    sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
    cd "$REPO_ROOT/qdistro"
    for pol in selinux/broker selinux/pwd selinux/tier1; do
        if [ -d "$pol" ] && [ -x "$pol/install-policy.sh" ]; then
            log "  -> $pol"
            (cd "$pol" && bash install-policy.sh) || warn "$pol install failed"
        fi
    done
}

# ---------------------------------------------------------------------------
# 12. qdwin session for admin
# ---------------------------------------------------------------------------
install_qdwin_session() {
    log "installing qdwin user session for admin..."
    # Pass QDISTRO_SRC so the helper can locate tier2/spawn-tier2.sh; otherwise it
    # defaults to /root/qdistro-src/qdistro and silently skips Tier-2 helper install
    # on non-/root source trees (e.g. /opt/qdistro-src).
    QDISTRO_SRC="$REPO_ROOT/qdistro" \
    bash "$REPO_ROOT/qdistro/scripts/install/install-qdwin-session-for-vm.sh" \
        "$REPO_ROOT/qdshell"
}

# ---------------------------------------------------------------------------
# 13. greetd: qdgreeter (tty3) + fallback (tty4)
# ---------------------------------------------------------------------------
configure_greetd() {
    if [ -n "$SKIP_GREETD" ]; then
        log "skipping greetd configuration (--skip-greetd)"
        return 0
    fi
    if ! command -v greetd >/dev/null 2>&1; then
        warn "greetd not installed; skipping greetd config"
        return 0
    fi
    log "configuring greetd: qdgreeter on tty3, LXQt+labwc fallback on tty4..."

    # _greeter system user. greetd's [default_session].user convention;
    # runs the qdgreeter UI without admin privileges. PAM does the
    # privilege handoff at start_session time. Provisioned as a NON-LOGIN
    # (nologin shell), NON-HOME (no home dir) system user so a compromise of
    # the greeter process has no shell, no home to drop persistence into, and
    # no uid in the human-user range.
    if ! getent passwd _greeter >/dev/null; then
        useradd --system --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin _greeter \
            || warn "useradd _greeter failed (already present from RPM scriptlet?)"
    else
        # Idempotent re-harden: if a prior/RPM provisioning gave _greeter a
        # login shell or a real home, pin it back to non-login / non-home.
        usermod --shell /usr/sbin/nologin --home /nonexistent _greeter 2>/dev/null || true
    fi

    # greetd config (tty3 — production qdgreeter path).
    install -d -m 0755 /etc/greetd
    install -m 0644 \
        "$REPO_ROOT/qdistro/deploy/greetd-config.toml" \
        /etc/greetd/config.toml

    # fallback config + unit (tty4 — escape hatch).
    install -m 0644 \
        "$REPO_ROOT/qdistro/deploy/greetd-config-fallback.toml" \
        /etc/greetd/config-fallback.toml
    install -m 0644 \
        "$REPO_ROOT/qdistro/deploy/greetd-fallback.service" \
        /etc/systemd/system/greetd-fallback.service

    # systemd hardening drop-in for the distro-packaged greetd.service.
    if [ -f "$REPO_ROOT/qdistro/deploy/greetd-hardening.conf" ]; then
        install -d -m 0755 /etc/systemd/system/greetd.service.d
        install -m 0644 \
            "$REPO_ROOT/qdistro/deploy/greetd-hardening.conf" \
            /etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf
        log "  installed greetd.service systemd hardening drop-in"
    fi

    # session launcher — what greetd execs as admin post-auth.
    install -m 0755 \
        "$REPO_ROOT/qdistro/deploy/qdwin-session-launcher.sh" \
        /usr/local/bin/qdwin-session-launcher

    # qdwin-session.target + sub-units (system-wide copy of the
    # production target; install-qdwin-session-for-vm.sh also installs
    # the noctalia-* per-user units used by the legacy path).
    install -m 0644 \
        "$REPO_ROOT/qdistro/deploy/qdwin-session.target" \
        /etc/systemd/user/qdwin-session.target 2>/dev/null || true
    install -m 0644 \
        "$REPO_ROOT/qdistro/deploy/qdwin-compositor.service" \
        /etc/systemd/user/qdwin-compositor.service 2>/dev/null || true
    install -m 0644 \
        "$REPO_ROOT/qdistro/deploy/qdshell.service" \
        /etc/systemd/user/qdshell.service 2>/dev/null || true

    systemctl daemon-reload
    systemctl enable greetd.service
    systemctl enable greetd-fallback.service \
        || warn "greetd-fallback.service enable failed"
    systemctl set-default graphical.target
}

# ---------------------------------------------------------------------------
# 14. First-boot smoke check
# ---------------------------------------------------------------------------
smoke_check() {
    log "running first-boot smoke checks..."
    local ok=1
    if [ -x /usr/bin/qdgreeter ]; then
        log "  /usr/bin/qdgreeter: OK"
    else
        warn "  /usr/bin/qdgreeter not found"
        ok=0
    fi
    if [ -f /etc/greetd/config.toml ]; then
        log "  /etc/greetd/config.toml: OK"
    else
        warn "  /etc/greetd/config.toml not found"
        ok=0
    fi
    # D-Bus name checks — services may not be up without reboot.
    # First decide whether the system bus is reachable at all: if busctl
    # produces no output we cannot distinguish "bus down" from "name not
    # registered", so report the bus as unreachable and skip name checks.
    # --acquired: only names with a live owner (a reachable bus still lists
    # many system names here, so non-empty output == bus reachable). Without
    # it busctl also lists activatable-but-not-running names, which would be
    # misreported as ONLINE. --no-legend drops the header row.
    local bus_names
    if bus_names="$(busctl --acquired list --no-pager --no-legend 2>/dev/null)" && [ -n "$bus_names" ]; then
        # Match the bus-name column exactly (busctl's first field) so the
        # dotted names are not treated as regex wildcards / substring matches.
        if printf '%s\n' "$bus_names" | awk '$1=="org.qdistro.AdminBroker1"{f=1} END{exit !f}'; then
            log "  broker: ONLINE"
        else
            warn "  broker registered but not owning its bus name yet (normal before reboot)"
        fi
        if printf '%s\n' "$bus_names" | awk '$1=="org.qdistro.SessionManager1"{f=1} END{exit !f}'; then
            log "  session-manager: ONLINE"
        else
            warn "  session-manager registered but not owning its bus name yet (normal before reboot)"
        fi
    else
        warn "  D-Bus system bus not reachable; skipping broker/session-manager name checks (normal before reboot)"
    fi
    [ "$ok" -eq 1 ] && log "smoke check OK" || warn "smoke check: some checks failed (see above)"
    return 0  # never exit non-zero from smoke_check
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    require_root
    detect_distro
    prompt_inputs

    log "summary:"
    log "  profile      : $QDISTRO_PROFILE$(is_dev && echo '  (throwaway shortcuts ENABLED)' || echo '  (hardened)')"
    log "  distro       : $DISTRO"
    log "  admin user   : admin (uid 1000)"
    log "  regular user : $REGULAR_USER (uid 1001)"
    log "  source root  : $REPO_ROOT (branch=$BRANCH)"
    if [ -z "$ASSUME_YES" ] && [ -z "$NONINTERACTIVE" ]; then
        read -r -p "Proceed? [y/N] " ans </dev/tty
        case "$ans" in y|Y|yes|YES) ;; *) die "aborted by user" ;; esac
    fi

    # Step 5: Install packages
    install_packages

    # Step 6: Detect btrfs (must run before create_users to set SKIP_SUBVOLUMES)
    detect_btrfs

    # Step 7: Create users (each user gets a subvolume home via create_user_subvolume)
    create_users

    # Step 8: System btrfs subvolumes (vaults, libvirt, containers)
    provision_subvolumes

    # Step 9: Fetch sources
    fetch_sources

    # Step 10: Build qdwin
    build_qdwin

    # Step 11: Build qdistro daemons
    build_qdistro_daemons

    # Step 12: Build qdshell QML plugin
    build_qdshell_plugin

    # Step 13: pip install Python apps
    pip_install_apps

    # Step 14: Install Python modules + systemd units via installer chain
    install_python_modules

    # Step 15: Install locker config
    install_locker_config_file

    # Step 16: SELinux policies
    install_selinux_policies

    # Step 17: qdwin session (sets up loginctl enable-linger admin)
    install_qdwin_session

    # Step 18: Install qdlocker systemd user service (needs linger from step 17)
    install_qdlocker_service

    # Step 19: greetd
    configure_greetd

    # Step 20 (opt-in): tier-4 guest base image bake
    if [ -n "$BUILD_TIER4_BASE" ]; then
        # build-guest-image.sh requires QDISTRO_VM_PASSWORD (the root password
        # baked into the tier-4 guest image). If it is unset the helper fails
        # deep inside, surfacing as an opaque non-fatal warning. Precheck it
        # here and skip early with a clear, actionable message — consistent with
        # this opt-in step's warn-and-continue behavior (a missing prerequisite
        # for an opt-in build should not abort the whole bootstrap).
        if [ -z "${QDISTRO_VM_PASSWORD:-}" ]; then
            warn "skipping tier-4 base build (--tier4-base): QDISTRO_VM_PASSWORD is unset"
            warn "  set QDISTRO_VM_PASSWORD (the root password for the tier-4 guest image) and re-run with --tier4-base"
        else
            log "building tier-4 guest base image (--tier4-base)..."
            bash "$REPO_ROOT/qdistro/tier4-vm-guest/build-guest-image.sh" \
                || warn "tier-4 base build failed; tier-4 VM apps will not work"
        fi
    fi

    # Step 21: Smoke check
    smoke_check

    log "DONE."
    log ""
    if [ -z "$SKIP_GREETD" ]; then
        log "Reboot to start qdistro:"
        log "  systemctl reboot"
        log ""
        log "On next boot: qdgreeter login appears on tty3."
        log "  Admin account: admin (uid 1000)"
        log "  Regular user:  $REGULAR_USER (uid 1001)"
        log ""
        log "To test the session before rebooting (requires loginctl enable-linger admin):"
        log "  runuser -l admin -c 'systemctl --user start qdwin-compositor.service'"
    else
        log "Bootstrap complete (greetd not configured — --skip-greetd was set)."
        log "Configure greetd manually or re-run without --skip-greetd to enable the login screen."
        log ""
        # On the skip-greetd path the system-wide qdwin-compositor.service /
        # qdwin-session.target units (installed by configure_greetd) are NOT
        # present. Only the per-user units from install_qdwin_session exist for
        # admin, so the valid test command targets noctalia-shell.service.
        log "To test the session before rebooting (requires loginctl enable-linger admin):"
        log "  runuser -l admin -c 'systemctl --user start noctalia-shell.service'"
    fi
}

# Run main() only when executed directly, not when sourced (the idempotency
# test harness sources this file to exercise individual functions with mocked
# privileged commands). ${BASH_SOURCE[0]} != $0 means we were sourced.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
