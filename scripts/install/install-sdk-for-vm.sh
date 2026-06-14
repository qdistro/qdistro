#!/bin/bash
# install-sdk-for-vm.sh — idempotent install of the qdistro_app SDK package
# (App1 receiver + app integration; recall.py ships present-but-disabled) onto a
# fresh-clone VM.
#
# Takes the SDK package dir as $1 (default /root/qdistro-src/qdistro/sdk/qdistro_app).
#
# Layout:
#   /usr/lib/python*/site-packages/qdistro_app/{__init__,app_receiver,recall}.py
#
# The SDK is dropped under the system Python's site-packages so
# `import qdistro_app` (and `from qdistro_app import AppReceiver`) works without
# PYTHONPATH gymnastics — the real GUI apps (qterminator/qnotebook/qfileman) and
# the P03 app-launcher round-trip claim org.qdistro.App1 session-bus names via
# qdistro_app.AppReceiver.
#
# NB: this install used to live inside install-recall-for-vm.sh, which `f3e13eb`
# (cut Recall from v1) stopped invoking — silently dropping the SDK with it. The
# SDK is NOT recall-specific (recall.py just rides along, disabled), so it now
# installs independently.
set -euo pipefail

SDK_SRC=${1:-/root/qdistro-src/qdistro/sdk/qdistro_app}
if [ ! -d "$SDK_SRC" ] || [ ! -f "$SDK_SRC/__init__.py" ]; then
    echo "[install-sdk] qdistro_app SDK not found at $SDK_SRC" >&2
    echo "       need $SDK_SRC/__init__.py" >&2
    exit 2
fi

# Find the python3 site-packages dir at runtime — Tumbleweed jumps minor
# versions across rebases, so /usr/lib/python3.13/site-packages might be
# /usr/lib/python3.14/... tomorrow.
PY_SITE=$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
install -d -m 0755 "$PY_SITE/qdistro_app"
for _sdk_py in "$SDK_SRC"/*.py; do
    install -m 0644 "$_sdk_py" "$PY_SITE/qdistro_app/"
done

echo "[install-sdk] OK — qdistro_app installed at $PY_SITE/qdistro_app"
