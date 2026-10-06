#!/usr/bin/env bash
set -euo pipefail
py=$(python3 -c 'import sys; print("python%d%d" % sys.version_info[:2])')
mapfile -t packages < <(sed -e '/^#/d' -e '/^$/d' -e "s/^python3-/$py-/" /recipe/host-packages.txt)
# PyQt development files replace busybox's bzip2/diff shims on the minimal base.
zypper -n install --no-recommends --force-resolution "${packages[@]}"
# Tumbleweed packages Quickshell as noctalia-qs and names its binary qs.
# The headless Process regression uses the upstream command name.
ln -s /usr/bin/qs /usr/local/bin/quickshell
# Build this small vendored binding into the image, not the user's Python tree.
python3 -m venv --system-site-packages /opt/host-python
VIRTUAL_ENV=/opt/host-python PATH=/opt/host-python/bin:$PATH SIP_SRC=/recipe/qtermwidget-pyqt bash /recipe/build-sip.sh
