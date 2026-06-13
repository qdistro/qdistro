#!/bin/bash
# enable-qdgreeter.sh — convert a freshly bootstrapped VM into the daily
# driver login path: greetd on tty3 running qdgreeter, with tty4 fallback.
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

log "installing greetd config and fallback..."
install -d -m 0755 /etc/greetd
install -m 0644 "$QD/deploy/greetd-config.toml" /etc/greetd/config.toml
install -m 0644 "$QD/deploy/greetd-config-fallback.toml" /etc/greetd/config-fallback.toml
install -m 0644 "$QD/deploy/greetd-fallback.service" /etc/systemd/system/greetd-fallback.service

install -d -m 0755 /etc/systemd/system/greetd.service.d
install -m 0644 "$QD/deploy/greetd-hardening.conf" \
    /etc/systemd/system/greetd.service.d/10-qdistro-hardening.conf

install -m 0755 "$QD/deploy/qdwin-session-launcher.sh" /usr/local/bin/qdwin-session-launcher
install -m 0755 "$QD/deploy/qdistro-startlxqtwayland.sh" /usr/local/bin/qdistro-startlxqtwayland
# The tty4 fallback launcher execs `labwc -S /usr/local/bin/qdistro-lxqt-session-wrap`,
# so its wrapper must be installed too or the hatch 203/EXECs at the labwc step.
install -m 0755 "$QD/deploy/qdistro-lxqt-session-wrap.sh" /usr/local/bin/qdistro-lxqt-session-wrap
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
systemctl enable greetd.service
# tty4 fallback escape hatch (greetd-fallback.service → passwordless admin
# LXQt+labwc). Enable it only when the full session stack is actually present
# (launcher + `labwc -S` wrapper + labwc + lxqt-session), so a greeter bake
# without the LXQt stack doesn't get an enabled-but-missing unit thrashing
# 203/EXEC on tty4 (the unit is Restart=always). GUI bakes that install
# labwc/lxqt get a working hatch; headless greeter bakes ship it disabled.
if [ -x /usr/local/bin/qdistro-startlxqtwayland ] \
    && [ -x /usr/local/bin/qdistro-lxqt-session-wrap ] \
    && command -v labwc >/dev/null 2>&1 \
    && command -v lxqt-session >/dev/null 2>&1; then
    systemctl enable greetd-fallback.service || true
else
    systemctl disable greetd-fallback.service 2>/dev/null || true
    log "greetd-fallback.service installed but NOT enabled (LXQt fallback stack absent on this bake)"
fi
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
