#!/bin/bash
# install-phone-for-vm.sh — idempotent install of qdistro-phone
# spec/18 Phase-8 MVP onto a fresh-clone VM.
#
# Sources from /root/phone-src/ (staged by fresh-vm-bootstrap.sh).
#
# Layout:
#   /usr/libexec/qdistro/qdistro_phone.py            # presence + push body
#   /usr/libexec/qdistro/qdistro_phone_daemon.py     # HTTP + queue + config
#   /usr/libexec/qdistro/qdistro_phone_cli.py        # CLI module
#   /usr/local/bin/qdistro-phone                     # CLI shim
#   /etc/systemd/system/qdistro-phone.service
#   /etc/qdistro/phone/                              # 0700 root:root
#   /var/lib/qdistro/phone/                          # 0700 root:root
#
# The service starts only when the operator drops
# /etc/qdistro/phone/qdistro-phone.conf carrying the explicit ntfy
# URL + HMAC secret (Phase-8 MVP: no default URL).
set -euo pipefail

SRC=${1:-/root/phone-src}
if [ ! -d "$SRC" ]; then
    echo "[install-phone] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB_QDISTRO=/usr/libexec/qdistro
DEST_BIN=/usr/local/bin
DEST_SYSD=/etc/systemd/system
DEST_ETC=/etc/qdistro/phone
DEST_VAR=/var/lib/qdistro/phone

install -d -m 0755 "$DEST_LIB_QDISTRO" "$DEST_BIN" "$DEST_SYSD"
install -d -m 0700 "$DEST_ETC"
install -d -m 0700 "$DEST_VAR"

install -m 0644 "$SRC/qdistro_phone.py"        "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_phone_daemon.py" "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_phone_cli.py"    "$DEST_LIB_QDISTRO/"

cat >"$DEST_BIN/qdistro-phone" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_phone_cli.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-phone"

install -m 0644 "$SRC/qdistro-phone.service" "$DEST_SYSD/"

systemctl daemon-reload >/dev/null 2>&1 || true

echo "[install-phone] OK"
