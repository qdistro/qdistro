#!/bin/bash
# enable-qdgreeter.sh — convert a freshly bootstrapped VM into the daily
# driver login path: greetd on tty3 running qdgreeter (production session).
#
# Intended to run inside the guest after fresh-vm-bootstrap.sh has unpacked
# source tarballs under /root/qdistro-src.

set -euo pipefail

SRC="${QDISTRO_SRC:-/root/qdistro-src}"
QD="$SRC/qdistro"
QDG="$SRC/qdgreeter"

log() { echo "[enable-qdgreeter] $*"; }

[ -d "$QD/deploy" ] || { echo "missing qdistro deploy files at $QD/deploy" >&2; exit 2; }
[ -f "$QDG/pyproject.toml" ] || { echo "missing qdgreeter sources at $QDG" >&2; exit 2; }

log "installing qdgreeter into /opt/qdgreeter..."
zypper -n install --no-recommends dejavu-fonts google-noto-coloremoji-fonts >/dev/null 2>&1 \
    || { echo "failed to install qdgreeter fonts" >&2; exit 3; }
fc-cache -f >/dev/null 2>&1 || true

rm -rf /opt/qdgreeter
install -d -m 0755 /opt/qdgreeter
cp -a "$QDG"/. /opt/qdgreeter/
find /opt/qdgreeter -type d -name __pycache__ -prune -exec rm -rf {} +

tmp_wrapper=$(mktemp)
cat > "$tmp_wrapper" <<'EOF'
#!/bin/sh
export PYTHONPATH=/opt/qdgreeter${PYTHONPATH:+:$PYTHONPATH}
export QDGREETER_LOG=${QDGREETER_LOG:-INFO}
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/qdgreeter/runtime}
export XDG_CACHE_HOME=/run/qdgreeter/cache
mkdir -p "$XDG_RUNTIME_DIR" "$XDG_CACHE_HOME" 2>/dev/null || true
exec /usr/bin/python3 -m qdgreeter.app "$@"
EOF
install -m 0755 "$tmp_wrapper" /usr/bin/qdgreeter
rm -f "$tmp_wrapper"
if [ ! -s /usr/bin/qdgreeter ]; then
    echo "installed /usr/bin/qdgreeter wrapper is empty" >&2
    exit 3
fi

log "installing greetd config..."
install -d -m 0755 /etc/greetd
install -m 0644 "$QD/deploy/greetd-config.toml" /etc/greetd/config.toml

install -d -m 0755 /etc/systemd/system/greetd.service.d
install -m 0644 "$QD/deploy/greetd-hardening.conf" \
    /etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf

install -m 0755 "$QD/deploy/qdwin-session-launcher.sh" /usr/local/bin/qdwin-session-launcher
install -d -m 0755 /etc/systemd/user
install -m 0644 "$QD/deploy/qdwin-session.target" /etc/systemd/user/qdwin-session.target
install -m 0644 "$QD/deploy/qdwin-compositor.service" /etc/systemd/user/qdwin-compositor.service
install -m 0644 "$QD/deploy/qdshell.service" /etc/systemd/user/qdshell.service

if ! getent passwd _greeter >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin _greeter
else
    usermod --shell /usr/sbin/nologin --home /nonexistent _greeter 2>/dev/null || true
fi

for g in video render input tty; do
    getent group "$g" >/dev/null && usermod -aG "$g" _greeter || true
done

install -d -o _greeter -g _greeter -m 0755 /run/qdgreeter

log "enabling greetd on tty3..."
systemctl daemon-reload
systemctl mask jeos-firstboot.service jeos-firstboot-snapshot.service 2>/dev/null || true
systemctl unmask greetd.service 2>/dev/null || true
loginctl disable-linger admin 2>/dev/null || true
loginctl terminate-user admin 2>/dev/null || true
systemctl stop user@1000.service 2>/dev/null || true
runuser -l admin -c \
    'XDG_RUNTIME_DIR=/run/user/1000 systemctl --user disable --now noctalia-session.service noctalia-shell.service 2>/dev/null || true' \
    || true
# Tear down any pre-existing tty4 LXQt+labwc fallback from an older bake/golden
# (the passwordless escape hatch has been removed). Idempotent — no-op on a clean
# tree. Critical when run against a REUSED VM golden (snapshot-daily) that could
# still carry the old passwordless greetd-fallback.service.
systemctl disable --now greetd-fallback.service 2>/dev/null || true
rm -f /etc/systemd/system/greetd-fallback.service /etc/greetd/config-fallback.toml
systemctl daemon-reload
systemctl enable greetd.service
install -d -m 0755 /etc/systemd/system/multi-user.target.wants
ln -sfn /usr/lib/systemd/system/greetd.service \
    /etc/systemd/system/multi-user.target.wants/greetd.service
systemctl reset-failed greetd.service 2>/dev/null || true
systemctl restart --no-block greetd.service
if [ ! -s /usr/bin/qdgreeter ]; then
    echo "installed /usr/bin/qdgreeter wrapper is empty after greeter setup" >&2
    exit 3
fi
sync
chvt 3 || true
