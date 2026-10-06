# shellcheck shell=bash
# qdistro-python.sh — the python3 interpreter pin.
#
# qdistro's app stack installs the versioned module set (python314-PyQt6,
# python314-dbus_next, ...) but every entry point — pip installs, systemd
# units, tests, helpers — invokes the unversioned `python3`. On Tumbleweed
# snapshots where python313-base still owns the /usr/bin/python3 symlink,
# installing python314-* alone leaves python3 pointing at an interpreter
# that cannot import any of the installed modules (or at nothing at all,
# when python313-base was never pulled in). Until TW flips the primary
# flavour to 3.14, the distro pins the name itself.
#
# Source (never execute) after the python314-* packages are installed:
#
#   . "$(dirname "$0")/lib/qdistro-python.sh"   # or absolute path
#   ensure_python3_314                          # dies via the caller's die()

ensure_python3_314() {
    if ! command -v python3.14 >/dev/null 2>&1; then
        echo "python314-base not installed; cannot pin /usr/bin/python3" >&2
        return 1
    fi
    if [ "$(readlink /usr/bin/python3 2>/dev/null)" != "python3.14" ]; then
        ln -sf python3.14 /usr/bin/python3
    fi
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 14), sys.version' \
        || { echo "/usr/bin/python3 did not resolve to Python 3.14" >&2; return 1; }
}
