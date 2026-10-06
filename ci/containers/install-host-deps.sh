#!/usr/bin/env bash
set -euo pipefail
py=$(python3 -c 'import sys; print("python%d%d" % sys.version_info[:2])')
mapfile -t packages < <(sed -e '/^#/d' -e '/^$/d' -e "s/^python3-/$py-/" /recipe/host-packages.txt)
# PyQt development files replace busybox's bzip2/diff shims on the minimal base.
zypper -n install --no-recommends --force-resolution "${packages[@]}"
# qdshell's runtime is the vendored upstream Quickshell, built once by the
# shared native-stage podman builder — /recipe/quickshell-bin holds its
# usr/bin output (quickshell + the qs symlink). Tumbleweed never shipped an
# upstream package; the archived noctalia-qs fork was the only provider.
cp -a /recipe/quickshell-bin/usr/bin/quickshell /recipe/quickshell-bin/usr/bin/qs \
    /usr/bin/
# Build this small vendored binding into the image, not the user's Python tree.
python3 -m venv --system-site-packages /opt/host-python
VIRTUAL_ENV=/opt/host-python PATH=/opt/host-python/bin:$PATH SIP_SRC=/recipe/qtermwidget-pyqt bash /recipe/build-sip.sh
