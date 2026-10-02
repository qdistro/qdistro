#!/bin/bash
# Install the non-graphical admin approval surfaces:
#   /usr/local/sbin/qdistro-approvals   root CLI: pending / approve / deny,
#                                       plus the cache + audit commands
#   /usr/local/bin/qdistro-admin-tui    Textual approval queue for the
#                                       uid-1000 admin on any terminal
#
# Both paths are the ones the broker's control-plane peer check admits
# (broker/qdistro_admin_broker.py: _ROOT_ADMIN_CONTROL_EXES for root,
# _ADMIN_CONTROL_SCRIPT_PATHS for uid 1000). The broker reads /proc/<pid>/
# exe, which for a Python script is the interpreter, so it also requires the
# installed script path in the process argv. The kernel puts the path the
# script was exec'd by into argv when it runs the shebang, so both files must
# be exec'd by these exact paths and must NOT be moved, wrapped in a shell
# script, or replaced by `python3 <copy>`. That argv match identifies the
# genuine tool; it is not a boundary against root or the admin uid (any
# process of that uid can name the path in argv). See doc/admin-approval.md.
#
# $1 is the repo root (the chain passes $QD). DESTDIR allows a host-only
# layout test; production paths remain absolute. File drops only: nothing
# here needs a running system, so the offline-install contract is trivially
# met (the library is still resolved, as for every chain installer).
set -euo pipefail

_QDO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-offline.sh
. "$_QDO_DIR/lib/qdistro-offline.sh"
resolve_offline_install

REPO_ROOT=${1:-/root/qdistro-src}
[ -d "$REPO_ROOT" ] || { echo "ERROR: source tree missing: $REPO_ROOT" >&2; exit 2; }
REPO_ROOT=$(cd "$REPO_ROOT" && pwd)
DESTDIR=${DESTDIR:-}
case "$DESTDIR" in
    ""|/*) ;;
    *) echo "ERROR: DESTDIR must be an absolute path" >&2; exit 2 ;;
esac
if [ "$DESTDIR" = / ]; then
    echo "ERROR: DESTDIR=/ is ambiguous; leave it unset for a live install" >&2
    exit 2
fi
if [ -z "$DESTDIR" ]; then
    [ "$(id -u)" = 0 ] || { echo "ERROR: root is required for a live install" >&2; exit 2; }
    OWN=(-o root -g root)
else
    OWN=()
fi

CLI=$REPO_ROOT/cli/qdistro_approvals.py
TUI_MODULES=(__init__.py broker_client.py silo_colors.py qdistro_admin_tui.py)
for source_file in "$CLI" "${TUI_MODULES[@]/#/$REPO_ROOT/tui/}"; do
    [ -f "$source_file" ] || { echo "ERROR: missing admin CLI/TUI source: $source_file" >&2; exit 2; }
done

TUI_LIB=/usr/local/lib/qdistro/admin-tui
install -d "${OWN[@]}" -m 0755 "$DESTDIR/usr/local/sbin" "$DESTDIR/usr/local/bin" \
    "$DESTDIR/usr/local/lib/qdistro" "$DESTDIR$TUI_LIB"

# Root CLI. 0755 like the other sbin tools; it refuses non-root itself and
# the broker refuses it as any uid but 0.
install "${OWN[@]}" -m 0755 "$CLI" "$DESTDIR/usr/local/sbin/qdistro-approvals"

# TUI: modules in a root-owned lib dir, entry point a symlink to the main
# module. The TUI puts its own resolved directory on sys.path, so the
# symlink finds broker_client/silo_colors; argv still carries the
# /usr/local/bin path the broker trusts.
for m in "${TUI_MODULES[@]}"; do
    mode=0644
    [ "$m" = qdistro_admin_tui.py ] && mode=0755
    install "${OWN[@]}" -m "$mode" "$REPO_ROOT/tui/$m" "$DESTDIR$TUI_LIB/$m"
done
ln -sfn "$TUI_LIB/qdistro_admin_tui.py" "$DESTDIR/usr/local/bin/qdistro-admin-tui"

# The image and machine bootstrap install these from distro packages
# (python313-dbus-python, python313-textual, python313-rich). Catch a
# packaging omission here while the strict image chain can fail. The CLI
# needs only dbus-python (always fatal); a missing Textual is fatal under
# QDISTRO_STRICT=1 (the image chain) and a warning otherwise, so an older
# test base without it still gets the CLI.
if [ -z "$DESTDIR" ]; then
    /usr/bin/python3 -c 'import dbus, dbus.mainloop.glib'
    if ! /usr/bin/python3 -c 'import textual, rich' 2>/dev/null; then
        if [ "${QDISTRO_STRICT:-0}" = 1 ]; then
            echo "ERROR: python313-textual/python313-rich missing; qdistro-admin-tui cannot start" >&2
            exit 1
        fi
        echo "WARN: python313-textual/python313-rich missing; qdistro-admin-tui will not start until installed" >&2
    fi
fi
echo "admin approval CLI (qdistro-approvals) and TUI (qdistro-admin-tui) installed"
