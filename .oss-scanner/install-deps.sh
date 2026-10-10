#!/usr/bin/env bash
set -euo pipefail
py=$(python3 -c 'import sys; print("python%d%d" % sys.version_info[:2])')
mapfile -t packages < <(sed -e '/^#/d' -e '/^$/d' -e "s/^python3-/$py-/" /recipe/host-packages.txt)
zypper -n install --no-recommends "${packages[@]}" \
    "${py}-textual" "${py}-rich" "${py}-pyte" "${py}-pyenchant" \
    "${py}-matplotlib" "${py}-networkx" "${py}-qrcode" \
    "${py}-setproctitle" "${py}-Pygments" "${py}-mcp" \
    "${py}-dbus_next" "${py}-pytest-asyncio" "${py}-pytest-timeout" \
    "${py}-tomli" myspell-en_US rage-encryption btrfsprogs \
    clang gdb strace valgrind curl ca-certificates jq shadow util-linux
python3 -m venv --system-site-packages /opt/scanner-python
mkdir -p /opt/scanner-tools
# Vendored wrappers call ninja without a job limit. Bound all Ninja builds,
# including their install steps, rather than relying on host CPU/RAM counts.
cat > /opt/scanner-tools/ninja <<'SH'
#!/usr/bin/env bash
exec /usr/bin/ninja -j "${OSS_SCANNER_JOBS:-2}" "$@"
SH
chmod +x /opt/scanner-tools/ninja
