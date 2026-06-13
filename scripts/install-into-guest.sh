#!/bin/bash
# install-into-guest.sh — overlay qdlocker onto an existing qdistro
# tier4 guest qcow2 image. Sibling to qdistro/tier4-vm/build-guest-
# image.sh; runs after the base build.
#
# What it adds:
#   - /opt/qdlocker (working copy of this repo)
#   - PyQt6 / dbus-next / python-pam / pywayland via pip
#   - systemd --user qdlocker.service enabled for the admin user
#   - /usr/libexec/qdistro-fprintd-fake stub used by tests/gui/02
#
# Usage:
#   ./install-into-guest.sh [--image PATH]
#
# Default --image is the tier4 base produced by build-guest-image.sh.
set -euo pipefail

IMG="${IMG:-/var/lib/libvirt/images/qdistro-tier4-base.qcow2}"

while [ $# -gt 0 ]; do
    case "$1" in
        --image) IMG="$2"; shift 2 ;;
        *) echo "usage: $0 [--image PATH]" >&2; exit 1 ;;
    esac
done

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -f "$IMG" ]; then
    echo "install-into-guest: image not found at $IMG" >&2
    echo "  build it first: cd ../../qdistro/tier4-vm && ./build-guest-image.sh" >&2
    exit 2
fi

for tool in virt-customize virt-copy-in; do
    command -v "$tool" >/dev/null || {
        echo "install-into-guest: missing $tool (zypper install guestfs-tools)" >&2
        exit 3
    }
done

echo "[qdlocker-install] copying repo into $IMG ..."
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Stage a clean tarball — exclude .git, build artefacts, caches.
tar --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=build --exclude=dist \
    -C "$REPO/.." -czf "$TMPDIR/qdlocker.tgz" qdlocker

cat >"$TMPDIR/qdistro-fprintd-fake.service" <<'UNIT'
[Unit]
Description=qdistro fake fprintd for VM tests

[Service]
Type=dbus
BusName=net.reactivated.Fprint
ExecStart=/usr/libexec/qdistro-fprintd-fake

[Install]
WantedBy=multi-user.target
UNIT

cat >"$TMPDIR/qdistro-fprintd-fake" <<'FAKE'
#!/usr/bin/env python3
"""Minimal fake net.reactivated.Fprint for VM tests. Exposes the
Manager + Device interfaces qdlocker calls into, plus a private
qdistro.FprintFake.EmitMatch method tests use to trigger a match.
"""
import asyncio
from dbus_next.service import ServiceInterface, method, signal
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType


class Manager(ServiceInterface):
    def __init__(self):
        super().__init__("net.reactivated.Fprint.Manager")

    @method()
    def GetDefaultDevice(self) -> "o":
        return "/net/reactivated/Fprint/Device/0"


class Device(ServiceInterface):
    def __init__(self):
        super().__init__("net.reactivated.Fprint.Device")

    @method()
    def Claim(self, username: "s"): pass
    @method()
    def Release(self): pass
    @method()
    def VerifyStart(self, finger: "s"): pass
    @method()
    def VerifyStop(self): pass

    @signal()
    def VerifyStatus(self, result: "s", done: "b") -> "sb":
        return [result, done]


class Fake(ServiceInterface):
    def __init__(self, device):
        super().__init__("qdistro.FprintFake")
        self._device = device

    @method()
    def EmitMatch(self):
        self._device.VerifyStatus("verify-match", True)


async def main():
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    mgr = Manager()
    dev = Device()
    fake = Fake(dev)
    bus.export("/net/reactivated/Fprint/Manager", mgr)
    bus.export("/net/reactivated/Fprint/Device/0", dev)
    bus.export("/net/reactivated/Fprint/Device/0", fake)
    await bus.request_name("net.reactivated.Fprint")
    await asyncio.Event().wait()


asyncio.run(main())
FAKE
chmod 0755 "$TMPDIR/qdistro-fprintd-fake"

# PyQt6, python-pam, dbus-next, pywayland: zypper-shipped versions
# on openSUSE Tumbleweed work, and using them avoids pulling the full
# build toolchain (gcc + python-devel + wayland-devel) for pip-built
# wheels. `--no-deps` on the qdlocker install relies on these being
# present.
virt-customize -a "$IMG" \
    --install python313-pip,python313-PyQt6,python313-python-pam,python313-dbus_next,python313-pywayland \
    --copy-in "$TMPDIR/qdlocker.tgz:/tmp/" \
    --run-command 'tar -C /opt -xzf /tmp/qdlocker.tgz && rm /tmp/qdlocker.tgz' \
    --run-command 'python3 -m pip install --break-system-packages --no-deps /opt/qdlocker' \
    --copy-in "$TMPDIR/qdistro-fprintd-fake:/usr/libexec/" \
    --copy-in "$TMPDIR/qdistro-fprintd-fake.service:/etc/systemd/system/" \
    --copy-in "$REPO/systemd/qdlocker.service:/etc/systemd/user/" \
    --run-command 'install -m 0644 -o root -g root /opt/qdlocker/pam/qdlocker /etc/pam.d/qdlocker' \
    --run-command 'install -d -o admin -g users /home/admin/.config/systemd/user/default.target.wants' \
    --run-command 'ln -snf /etc/systemd/user/qdlocker.service /home/admin/.config/systemd/user/default.target.wants/qdlocker.service' \
    >/dev/null

# qdistro-fprintd-fake is staged but NOT enabled — it claims the
# same bus name as the real fprintd and would race on boot. Tests
# that need it call `systemctl start qdistro-fprintd-fake.service`
# AFTER stopping fprintd; see qdlocker/tests/gui/02-fprintd-fallback.md.

echo "[qdlocker-install] done."
