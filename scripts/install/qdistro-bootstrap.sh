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
#       qdbrowser, qdgreeter) — uses sibling checkouts under $REPO_ROOT
#       if present, otherwise clones from codeberg.org/qdistro.
#   7.  Builds qdwin (libweston shell plugin) and the qdistro C daemons.
#   8.  Builds the qdshell QML plugin (libqdistro-qdwin.so).
#   9.  pip-installs qdgreeter, qdlocker, qdbrowser.
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
# CLI flags (all override the matching env var below):
#   --admin-password=PW    set admin's password (uid 1000)
#   --user=NAME            set the regular username (uid 1001)
#   --user-password=PW     set the regular user's password
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
#   --skip-build           skip all build steps (qdwin, daemons, qdshell, pip)
#   --tier4-base           opt-in: build the tier-4 guest base qcow2 image
#   -h, --help             show help and exit
#
# Equivalent env vars (CLI wins): QDISTRO_ADMIN_PASSWORD,
#   QDISTRO_USER_NAME, QDISTRO_USER_PASSWORD, QDISTRO_REPO_ROOT,
#   QDISTRO_BRANCH, QDISTRO_NONINTERACTIVE, QDISTRO_YES,
#   QDISTRO_SKIP_PACKAGES, QDISTRO_SKIP_GREETD,
#   QDISTRO_SKIP_SUBVOLUMES, QDISTRO_SKIP_SOURCES,
#   QDISTRO_SKIP_BUILD, QDISTRO_BUILD_TIER4_BASE.

set -euo pipefail

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(dirname "$SCRIPT_PATH")
# scripts/install/<this> -> qdistro/ -> qdistro-org/
QDISTRO_DIR_DEFAULT=$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || true)
REPO_ROOT="${QDISTRO_REPO_ROOT:-${QDISTRO_DIR_DEFAULT:+$(dirname "$QDISTRO_DIR_DEFAULT")}}"
REPO_ROOT="${REPO_ROOT:-/opt/qdistro-src}"
BRANCH="${QDISTRO_BRANCH:-main}"

DISTRO=""           # "tumbleweed" | "ubuntu"
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

log()  { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root (try: sudo $0)"
}

print_help() {
    sed -n '2,62p' "$SCRIPT_PATH"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --admin-password=*)  ADMIN_PASSWORD="${1#*=}" ;;
            --admin-password)    shift; ADMIN_PASSWORD="$1" ;;
            --user=*)            REGULAR_USER="${1#*=}" ;;
            --user)              shift; REGULAR_USER="$1" ;;
            --user-password=*)   REGULAR_PASSWORD="${1#*=}" ;;
            --user-password)     shift; REGULAR_PASSWORD="$1" ;;
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
            -h|--help)           print_help; exit 0 ;;
            *) die "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done

    # Fail fast: in noninteractive mode, missing required fields are an
    # immediate error before we even check root. This allows the test
    # suite to verify the error message without running as root.
    if [ -n "$NONINTERACTIVE" ]; then
        local missing=0
        [ -z "$ADMIN_PASSWORD" ]   && missing=1
        [ -z "$REGULAR_USER" ]     && missing=1
        [ -z "$REGULAR_PASSWORD" ] && missing=1
        if [ "$missing" -eq 1 ]; then
            die "noninteractive mode but missing values; supply --admin-password, --user, --user-password"
        fi
        # Validate username format early too (before root check)
        if ! printf '%s' "$REGULAR_USER" | grep -qE '^[a-z_][a-z0-9_-]*$'; then
            die "invalid username '$REGULAR_USER' (lowercase, start with letter/underscore)"
        fi
        if [ "$REGULAR_USER" = "admin" ] || [ "$REGULAR_USER" = "root" ]; then
            die "regular user must not be 'admin' or 'root'"
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
    # If a value is already supplied (CLI flag or env var), keep it;
    # otherwise prompt — unless --noninteractive, in which case fail.
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
    zypper -n --no-gpg-checks refresh >/dev/null

    # Reuse the canonical Tumbleweed list from install-deps.sh.
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/install-deps.sh"
    local extra=( git wget curl openssh tar gzip xz )
    log "installing ${#QDISTRO_PKGS[@]} build/runtime packages + ${#extra[@]} extras..."
    zypper -n install --no-recommends "${QDISTRO_PKGS[@]}" "${extra[@]}"

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
    apt-get update -qq

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

    apt-get install -y --no-install-recommends "${available[@]}"

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
# 4. Btrfs subvolume helpers
# ---------------------------------------------------------------------------
provision_subvolumes() {
    if [ -n "$SKIP_SUBVOLUMES" ]; then
        log "skipping subvolume provisioning (--skip-subvolumes)"
        return 0
    fi
    local fstype
    fstype=$(findmnt -o FSTYPE / --noheadings 2>/dev/null | tr -d ' ' || echo unknown)
    if [ "$fstype" != "btrfs" ]; then
        warn "root filesystem is $fstype, not btrfs — subvolume layout skipped (snapshot features will not work)"
        SKIP_SUBVOLUMES=1
        return 0
    fi
    log "provisioning btrfs subvolumes..."
    for path in /var/lib/qdistro/vaults /var/lib/libvirt/images /var/lib/containers; do
        install -d -m 0755 "$(dirname "$path")"
        if btrfs subvolume show "$path" >/dev/null 2>&1; then
            log "  $path: already a subvolume"
        elif [ -d "$path" ] && [ "$(ls -A "$path" 2>/dev/null)" ]; then
            warn "  $path: directory exists and is non-empty, not converting to subvolume"
        else
            [ -d "$path" ] && rmdir "$path" 2>/dev/null || true
            btrfs subvolume create "$path"
            log "  $path: created"
        fi
    done

    # Snapper config for vaults subvolume
    if command -v snapper >/dev/null 2>&1; then
        if ! snapper list-configs 2>/dev/null | grep -q "qdistro-vaults"; then
            snapper -c qdistro-vaults create-config /var/lib/qdistro/vaults 2>/dev/null \
                || warn "  snapper vault config failed (non-fatal)"
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
        warn "  /home/$user: directory exists and is non-empty, not converting to subvolume"
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
            snapper -c "home-${user}" create-config "$home" 2>/dev/null \
                || warn "  snapper config for $user failed (non-fatal)"
        fi
    fi
}

# ---------------------------------------------------------------------------
# 5. Users
# ---------------------------------------------------------------------------
create_users() {
    log "creating admin user (uid 1000)..."
    if ! getent passwd admin >/dev/null; then
        # Do not use -m; we handle home creation via create_user_subvolume
        useradd -M -u 1000 -U -s /bin/bash admin
    fi
    create_user_subvolume admin 1000
    echo "admin:${ADMIN_PASSWORD}" | chpasswd
    # wheel group on Tumbleweed, sudo on Ubuntu
    if getent group wheel >/dev/null; then usermod -aG wheel admin; fi
    if getent group sudo  >/dev/null; then usermod -aG sudo  admin; fi
    install -m 0440 /dev/stdin /etc/sudoers.d/99-admin <<<'admin ALL=(ALL) NOPASSWD: ALL'

    log "creating regular user '$REGULAR_USER' (uid 1001)..."
    if ! getent passwd "$REGULAR_USER" >/dev/null; then
        # Do not use -m; we handle home creation via create_user_subvolume
        useradd -M -u 1001 -U -s /bin/bash "$REGULAR_USER"
    fi
    create_user_subvolume "$REGULAR_USER" 1001
    echo "${REGULAR_USER}:${REGULAR_PASSWORD}" | chpasswd
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
fetch_sources() {
    if [ -n "$SKIP_SOURCES" ]; then
        log "skipping source acquisition (--skip-sources)"
        return 0
    fi
    install -d -m 0755 "$REPO_ROOT"
    cd "$REPO_ROOT"

    # Core repos: detect by .git dir, meson.build, or daemons/ subdir.
    for repo in qdistro qdwin qdshell; do
        if [ -d "$REPO_ROOT/$repo/.git" ] || [ -d "$REPO_ROOT/$repo/daemons" ] \
           || [ -f "$REPO_ROOT/$repo/meson.build" ]; then
            log "  $repo: using existing checkout at $REPO_ROOT/$repo"
            continue
        fi
        log "  $repo: cloning from codeberg.org/qdistro/$repo (branch=$BRANCH)..."
        git clone --depth 1 --branch "$BRANCH" \
            "https://codeberg.org/qdistro/$repo.git" "$REPO_ROOT/$repo"
    done

    # Python app repos: detect by pyproject.toml as existence sentinel.
    for repo in qdlocker qdbrowser qdgreeter; do
        if [ -f "$REPO_ROOT/$repo/pyproject.toml" ]; then
            log "  $repo: using existing checkout at $REPO_ROOT/$repo"
            continue
        fi
        log "  $repo: cloning from codeberg.org/qdistro/$repo (branch=$BRANCH)..."
        git clone --depth 1 --branch "$BRANCH" \
            "https://codeberg.org/qdistro/$repo.git" "$REPO_ROOT/$repo" \
            || warn "  $repo: clone failed (non-fatal; pip install will be skipped)"
    done

    [ -f "$REPO_ROOT/qdwin/meson.build" ]   || die "qdwin tree missing meson.build"
    [ -d "$REPO_ROOT/qdistro/daemons" ]     || die "qdistro tree missing daemons/"
}

# ---------------------------------------------------------------------------
# 7. Build qdwin + qdistro daemons
# ---------------------------------------------------------------------------
build_qdwin() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping qdwin build (--skip-build)"
        return 0
    fi
    log "building qdwin (libweston shell plugin)..."
    cd "$REPO_ROOT/qdwin"
    meson setup build --wipe --prefix=/usr
    meson compile -C build
    meson install -C build
}

build_qdistro_daemons() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping qdistro daemons build (--skip-build)"
        return 0
    fi
    log "building qdistro C daemons..."
    cd "$REPO_ROOT/qdistro/daemons"
    meson setup build --wipe --prefix=/usr
    meson compile -C build
    meson install -C build
}

build_qdshell_plugin() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping qdshell QML plugin build (--skip-build)"
        return 0
    fi
    log "building qdshell QML plugin (libqdistro-qdwin.so)..."
    cd "$REPO_ROOT/qdshell"
    meson setup build --wipe --prefix=/usr \
        || die "qdshell meson setup failed"
    meson compile -C build \
        || die "qdshell meson compile failed"
    meson install -C build \
        || die "qdshell meson install failed"
}

pip_install_apps() {
    if [ -n "$SKIP_BUILD" ]; then
        log "skipping pip install of apps (--skip-build)"
        return 0
    fi
    log "installing Python apps (qdgreeter, qdlocker, qdbrowser)..."
    for app in qdgreeter qdlocker qdbrowser; do
        if [ -f "$REPO_ROOT/$app/pyproject.toml" ]; then
            log "  pip install $app..."
            python3 -m pip install --break-system-packages --no-deps --quiet \
                "$REPO_ROOT/$app" \
                || warn "  pip install $app failed (non-fatal)"
        else
            warn "  $app source not found at $REPO_ROOT/$app"
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
        "scripts/install/install-phone-for-vm.sh           $QD/phone"
        "scripts/install/install-print-proxy-for-vm.sh     $QD/print"
        "scripts/install/install-recall-for-vm.sh          $QD"
        "scripts/install/install-snapshots-for-vm.sh       $QD/snapshots"
        "scripts/install/install-tier3-for-vm.sh           $QD"
        "scripts/install/install-tier5-for-vm.sh           $QD"
    )
    cd "$QD"
    for entry in "${installers[@]}"; do
        # shellcheck disable=SC2086
        set -- $entry
        local installer="$1" src_dir="$2"
        if [ -x "$installer" ]; then
            log "  -> $(basename "$installer")"
            bash "$installer" "$src_dir" \
                || warn "$installer failed; continuing"
        fi
    done
}

# ---------------------------------------------------------------------------
# 9. qdlocker systemd user service
# ---------------------------------------------------------------------------
install_qdlocker_service() {
    local locker_src="$REPO_ROOT/qdlocker"
    [ -d "$locker_src/systemd" ] || { warn "qdlocker systemd dir not found"; return 0; }
    install -d -m 0755 /home/admin/.config/systemd/user
    install -m 0644 "$locker_src/systemd/qdlocker.service" \
        /home/admin/.config/systemd/user/qdlocker.service
    chown -R admin:users /home/admin/.config/systemd
    install -d -m 0755 /etc/systemd/user/qdlocker.service.d
    cat > /etc/systemd/user/qdlocker.service.d/qdshell-path.conf <<'EOF'
[Service]
Environment=QDLOCKER_QDSHELL_PATH=/usr/share/quickshell/qdshell
EOF
    runuser -l admin -c 'systemctl --user daemon-reload' 2>/dev/null || true
    runuser -l admin -c 'systemctl --user enable qdlocker.service' 2>/dev/null \
        || warn "qdlocker service enable failed (admin loginctl linger may be needed)"
    log "qdlocker service installed and enabled for admin"
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
    # privilege handoff at start_session time.
    if ! getent passwd _greeter >/dev/null; then
        useradd --system --no-create-home --shell /usr/sbin/nologin _greeter \
            || warn "useradd _greeter failed (already present from RPM scriptlet?)"
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
    # D-Bus name checks — services may not be up without reboot
    if busctl list --no-pager 2>/dev/null | grep -q com.qdistro.Broker1; then
        log "  broker: ONLINE"
    else
        warn "  broker not on bus (normal before reboot)"
    fi
    if busctl list --no-pager 2>/dev/null | grep -q com.qdistro.SessionManager1; then
        log "  session-manager: ONLINE"
    else
        warn "  session-manager not on bus (normal before reboot)"
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

    # Step 6: Create users (each user gets a subvolume home via create_user_subvolume)
    create_users

    # Step 7: System btrfs subvolumes (vaults, libvirt, containers)
    provision_subvolumes

    # Step 8: Fetch sources
    fetch_sources

    # Step 9: Build qdwin
    build_qdwin

    # Step 10: Build qdistro daemons
    build_qdistro_daemons

    # Step 11: Build qdshell QML plugin
    build_qdshell_plugin

    # Step 12: pip install Python apps
    pip_install_apps

    # Step 13: Install Python modules + systemd units via installer chain
    install_python_modules

    # Step 14: Install qdlocker systemd user service
    install_qdlocker_service

    # Step 15: Install locker config
    install_locker_config_file

    # Step 16: SELinux policies
    install_selinux_policies

    # Step 17: qdwin session
    install_qdwin_session

    # Step 18: greetd
    configure_greetd

    # Step 19 (opt-in): tier-4 guest base image bake
    if [ -n "$BUILD_TIER4_BASE" ]; then
        log "building tier-4 guest base image (--tier4-base)..."
        bash "$REPO_ROOT/qdistro/tier4-vm-guest/build-guest-image.sh" \
            || warn "tier-4 base build failed; tier-4 VM apps will not work"
    fi

    # Step 20: Smoke check
    smoke_check

    log "DONE."
    log ""
    log "Reboot to start qdistro:"
    log "  systemctl reboot"
    log ""
    log "On next boot: qdgreeter login appears on tty3."
    log "  Admin account: admin (uid 1000)"
    log "  Regular user:  $REGULAR_USER (uid 1001)"
    log ""
    log "To test before rebooting (requires a running user session):"
    log "  loginctl enable-linger admin"
    log "  runuser -l admin -c 'systemctl --user start qdwin-compositor.service'"
}

main "$@"
