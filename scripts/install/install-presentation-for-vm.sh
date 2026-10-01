#!/bin/bash
# install-presentation-for-vm.sh — public presentation snapshot directory,
# deployment metadata, and the qdistro_presentation Python package.
#
# Takes the presentation package dir as $1 (default
# /root/qdistro-src/sdk/presentation/qdistro_presentation).
# DESTDIR (unset in production) prefixes every path so a host-only layout
# test can run without root or a live admin account.
set -euo pipefail

_QDO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-offline.sh
. "$_QDO_DIR/lib/qdistro-offline.sh"
resolve_offline_install

PRES_SRC=${1:-/root/qdistro-src/sdk/presentation/qdistro_presentation}
if [ ! -d "$PRES_SRC" ] || [ ! -f "$PRES_SRC/__init__.py" ]; then
    echo "[install-presentation] package not found at $PRES_SRC" >&2
    echo "       need $PRES_SRC/__init__.py" >&2
    exit 2
fi

DESTDIR=${DESTDIR:-}
case "$DESTDIR" in
    ""|/*) ;;
    *) echo "[install-presentation] DESTDIR must be an absolute path" >&2; exit 2 ;;
esac
if [ "$DESTDIR" = / ]; then
    echo "[install-presentation] DESTDIR=/ is ambiguous; leave it unset for a live install" >&2
    exit 2
fi

if [ -z "$DESTDIR" ]; then
    if ! getent passwd admin >/dev/null; then
        echo "[install-presentation] admin account is missing" >&2
        exit 2
    fi
    ADMIN_UID=$(id -u admin)
    if [ "$ADMIN_UID" -ne 1000 ]; then
        echo "[install-presentation] admin is uid $ADMIN_UID, expected 1000" >&2
        exit 2
    fi
    OWN_ROOT=(-o root -g root)
    OWN_ADMIN=(-o admin -g admin)
else
    ADMIN_UID=1000
    OWN_ROOT=()
    OWN_ADMIN=()
fi

install -d "${OWN_ROOT[@]}" -m 0755 "$DESTDIR/var/lib/qdistro"
install -d "${OWN_ADMIN[@]}" -m 0755 "$DESTDIR/var/lib/qdistro/presentation"
# Never write a default current.json: absence means native fallback.

install -d "${OWN_ROOT[@]}" -m 0755 "$DESTDIR/usr/share/qdistro/presentation" \
    "$DESTDIR/usr/bin"
umask 022
META_PATH="$DESTDIR/usr/share/qdistro/presentation/deployment.json"
python3 - "$ADMIN_UID" "$META_PATH" "$DESTDIR" <<'PY'
import json, os, sys
uid = int(sys.argv[1])
path = sys.argv[2]
destdir = sys.argv[3]
payload = json.dumps({"version": 1, "admin_uid": uid}, separators=(",", ":")) + "\n"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
try:
    os.write(fd, payload.encode("utf-8"))
    if not destdir:
        os.fchown(fd, 0, 0)
    os.fchmod(fd, 0o644)
finally:
    os.close(fd)
PY

PY_SITE=$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
install -d -m 0755 "$DESTDIR$PY_SITE/qdistro_presentation"
for _py in "$PRES_SRC"/*.py; do
    install -m 0644 "$_py" "$DESTDIR$PY_SITE/qdistro_presentation/"
done

cat > "$DESTDIR/usr/bin/qdistro-presentation-publish" <<'EOF'
#!/usr/bin/python3
from qdistro_presentation.cli import main
raise SystemExit(main())
EOF
chmod 0755 "$DESTDIR/usr/bin/qdistro-presentation-publish"

if [ -z "$DESTDIR" ] && command -v restorecon >/dev/null 2>&1; then
    restorecon -RF /var/lib/qdistro/presentation 2>/dev/null || true
    restorecon -F /usr/share/qdistro/presentation/deployment.json 2>/dev/null || true
fi

echo "[install-presentation] OK — dir $DESTDIR/var/lib/qdistro/presentation (admin:$ADMIN_UID) + $DESTDIR$PY_SITE/qdistro_presentation"
