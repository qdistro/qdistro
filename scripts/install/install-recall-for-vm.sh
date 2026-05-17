#!/bin/bash
# install-recall-for-vm.sh — idempotent install of qdistro-recall
# Phase-8 §step 0 MVP onto a fresh-clone VM.
#
# Takes the umbrella root as $1 (default /root/qdistro-src/qdistro).
# Recall is unusual: its files are spread across three subtrees of
# the umbrella — recall/ (daemon + ingest + units), cli/ (CLI), and
# sdk/qdistro_app/ (SDK module). We pull from all three.
#
# Layout:
#   /usr/libexec/qdistro/qdistro_recall_ingest.py      # engine
#   /usr/libexec/qdistro/qdistro_recall_daemon.py      # TTL reaper
#   /usr/local/bin/qdistro-recall                      # host CLI
#   /etc/systemd/system/qdistro-recall@.service
#   /etc/systemd/system/qdistro-recall@.timer
#   /var/lib/qdistro/recall/                           # 0755 root
#   /var/lib/qdistro/recall/admin/                       # 0700 admin:admin
#   /usr/lib/python*/site-packages/qdistro_app/recall.py
#
# The SDK is dropped under the system Python's site-packages so
# `import qdistro_app.recall` works without PYTHONPATH gymnastics.
set -euo pipefail

UMBRELLA=${1:-/root/qdistro-src/qdistro}
if [ ! -d "$UMBRELLA/recall" ] || [ ! -d "$UMBRELLA/cli" ] || [ ! -d "$UMBRELLA/sdk/qdistro_app" ]; then
    echo "[install-recall] umbrella tree incomplete at $UMBRELLA" >&2
    echo "       need $UMBRELLA/{recall,cli,sdk/qdistro_app}/" >&2
    exit 2
fi

RECALL_SRC="$UMBRELLA/recall"
CLI_SRC="$UMBRELLA/cli"
SDK_SRC="$UMBRELLA/sdk/qdistro_app"

DEST_LIB_QDISTRO=/usr/libexec/qdistro
DEST_BIN=/usr/local/bin
DEST_SYSD=/etc/systemd/system
DEST_VAR=/var/lib/qdistro/recall

install -d -m 0755 "$DEST_LIB_QDISTRO" "$DEST_BIN" "$DEST_SYSD"

# Engine + reaper modules.
install -m 0644 "$RECALL_SRC/qdistro_recall_ingest.py"  "$DEST_LIB_QDISTRO/"
install -m 0644 "$RECALL_SRC/qdistro_recall_daemon.py"  "$DEST_LIB_QDISTRO/"

# Host CLI shim (so `qdistro-recall search ...` works for any user).
cat >"$DEST_BIN/qdistro-recall" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_recall_cli.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-recall"
install -m 0644 "$CLI_SRC/qdistro_recall_cli.py" "$DEST_LIB_QDISTRO/"

# systemd instance unit + timer.
install -m 0644 "$RECALL_SRC/qdistro-recall@.service" "$DEST_SYSD/"
install -m 0644 "$RECALL_SRC/qdistro-recall@.timer"   "$DEST_SYSD/"

# SDK drop under site-packages. Find the python3 sitepackages dir
# at runtime — Tumbleweed jumps minor versions across rebases, so
# /usr/lib/python3.13/site-packages might be /usr/lib/python3.14/...
# tomorrow.
PY_SITE=$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
install -d -m 0755 "$PY_SITE/qdistro_app"
# Don't clobber the existing __init__.py if the SDK is already
# installed (e.g. from a future qdistro_app RPM); just drop the
# recall submodule.
install -m 0644 "$SDK_SRC/recall.py" "$PY_SITE/qdistro_app/"
if [ ! -f "$PY_SITE/qdistro_app/__init__.py" ]; then
    install -m 0644 "$SDK_SRC/__init__.py" \
        "$PY_SITE/qdistro_app/__init__.py"
fi

# Per-user recall dirs. /var/lib/qdistro/recall is 0755 root:root
# (created above). Per-user subdirs are 0700 owned by each user.
# Phase-8 only needs the dev box's admin (`admin`); production grows
# this list via spec/19 §"Per-user-home subvolumes" provisioning.
install -d -m 0755 "$DEST_VAR"
if id admin >/dev/null 2>&1; then
    install -d -m 0700 -o admin -g admin "$DEST_VAR/admin"
fi

# Enable the timer for admin if present. Skipped on hosts without the
# user (e.g. the bake's pre-user provisioning pass).
if id admin >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable --now qdistro-recall@admin.timer >/dev/null 2>&1 || true
fi

echo "[install-recall] OK"
