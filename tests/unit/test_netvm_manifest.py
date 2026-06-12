"""Unit tests for the net-VM manifest + fail-closed activation guard (piece 2).

The guard is pure, so we (a) prove the shipped ``net-vm/netvm-manifest.json``
parses and the shipped ``net-vm/domain-template.xml`` activates clean once
rendered, then (b) assert each undeclared-device class is blocked. This is the
``virsh define`` gate the install script runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from qdistro_metadata_schema import MetadataSchemaError
from qdistro_netvm_manifest import (
    NetVMManifest, parse, validate_manifest, validate_domain,
    assert_activatable, validate_usb_attach,
)

_NETVM_DIR = Path(__file__).resolve().parents[2] / "net-vm"


def _load_manifest_dict() -> dict:
    return json.loads((_NETVM_DIR / "netvm-manifest.json").read_text())


def _render_template(manifest: dict, **overrides) -> str:
    xml = (_NETVM_DIR / "domain-template.xml").read_text()
    subs = {
        "__VM_NAME__": "qdistro-netvm",
        "__MEM_KIB__": "262144",
        "__MGMT_MAC__": manifest["controlPlane"]["mgmtMac"],
        "__WAN_MAC__": manifest["interfaces"]["wanMac"],
        "__MGMT_NET__": "qdistro-netvm-mgmt",
        "__DISK_PATH__": "/var/lib/libvirt/images/qdistro-netvm.qcow2",
    }
    subs.update(overrides)
    for k, v in subs.items():
        xml = xml.replace(k, v)
    return xml


def _add_device(xml: str, snippet: str) -> str:
    """Insert a device snippet before </devices>."""
    return xml.replace("</devices>", snippet + "\n</devices>")


@pytest.fixture
def mdict():
    return _load_manifest_dict()


@pytest.fixture
def manifest(mdict) -> NetVMManifest:
    return parse(mdict)


# ---------------------------------------------------------------------------
# Shipped artifacts are self-consistent
# ---------------------------------------------------------------------------
class TestShipped:
    def test_manifest_parses(self, mdict):
        assert validate_manifest(mdict).ok
        m = parse(mdict)
        assert m.name == "qdistro-netvm"
        assert m.transport == "rpcd-http"
        assert m.exposes("qdistro.netvm", "egress_reload")
        assert m.exposes("iwinfo", "scan")
        assert not m.exposes("file", "exec")          # not granted

    def test_rendered_template_activates_clean(self, mdict, manifest):
        xml = _render_template(mdict)
        res = validate_domain(manifest, xml)
        assert res.ok, res.errors
        # assert_activatable must not raise on the shipped pair.
        assert_activatable(manifest, xml)

    def test_template_is_headless(self, mdict):
        xml = _render_template(mdict)
        root = ET.fromstring(xml)
        assert root.find("devices/graphics") is None
        assert root.find("devices/video") is None


# ---------------------------------------------------------------------------
# Undeclared devices are blocked (the load-bearing guard)
# ---------------------------------------------------------------------------
class TestActivationGuard:
    def test_undeclared_interface_mac_blocked(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<interface type='user'><mac address='52:54:00:de:ad:00'/>"
                          "<model type='virtio'/></interface>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("undeclared <interface>" in e for e in res.errors)

    def test_declared_per_silo_vif_ok(self, mdict, manifest):
        # MAC under siloVifPrefix with slot 0x05 < maxSiloVifs(64) is declared.
        xml = _add_device(_render_template(mdict),
                          "<interface type='network'><source network='siloA'/>"
                          "<mac address='52:54:00:9c:01:05'/>"
                          "<model type='virtio'/></interface>")
        assert validate_domain(manifest, xml).ok

    def test_per_silo_vif_over_budget_blocked(self, mdict, manifest):
        # slot 0x99 (153) exceeds maxSiloVifs(64) — undeclared.
        xml = _add_device(_render_template(mdict),
                          "<interface type='network'><source network='siloX'/>"
                          "<mac address='52:54:00:9c:01:99'/>"
                          "<model type='virtio'/></interface>")
        assert not validate_domain(manifest, xml).ok

    def test_vsock_blocked_under_rpcd_transport(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<vsock model='virtio'><cid auto='no' address='7'/></vsock>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("undeclared <vsock>" in e for e in res.errors)

    def test_graphics_blocked(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<graphics type='vnc' port='-1'/>")
        assert not validate_domain(manifest, xml).ok

    def test_undeclared_usb_hostdev_blocked(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<hostdev mode='subsystem' type='usb'><source>"
                          "<vendor id='0x0bda'/><product id='0x8812'/>"
                          "</source></hostdev>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("undeclared USB hostdev 0bda:8812" in e for e in res.errors)

    def test_declared_usb_hostdev_ok(self, mdict):
        mdict["usbHostdevAllow"] = [{"vendor": "0bda", "product": "8812"}]
        manifest = parse(mdict)
        xml = _add_device(_render_template(mdict),
                          "<hostdev mode='subsystem' type='usb'><source>"
                          "<vendor id='0x0bda'/><product id='0x8812'/>"
                          "</source></hostdev>")
        assert validate_domain(manifest, xml).ok

    def test_undeclared_pci_hostdev_blocked(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<hostdev mode='subsystem' type='pci'><source><address "
                          "domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>"
                          "</source></hostdev>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("0000:03:00.0" in e for e in res.errors)

    def test_declared_pci_hostdev_ok(self, mdict):
        mdict["pciPassthrough"] = [{"address": "0000:03:00.0"}]
        manifest = parse(mdict)
        xml = _add_device(_render_template(mdict),
                          "<hostdev mode='subsystem' type='pci'><source><address "
                          "domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>"
                          "</source></hostdev>")
        assert validate_domain(manifest, xml).ok

    def test_unknown_device_blocked(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<smartcard mode='passthrough'/>")
        assert not validate_domain(manifest, xml).ok

    def test_assert_activatable_raises(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<graphics type='vnc' port='-1'/>")
        with pytest.raises(MetadataSchemaError):
            assert_activatable(manifest, xml)

    def test_malformed_xml_blocked(self, manifest):
        with pytest.raises(MetadataSchemaError):
            assert_activatable(manifest, "<domain><devices>")

    def test_second_disk_blocked(self, mdict, manifest):
        # A second <disk> (a host file/blockdev) is undeclared even though the
        # tag is "baseline" — multiplicity cap (codex finding 1).
        xml = _add_device(_render_template(mdict),
                          "<disk type='file' device='disk'>"
                          "<source file='/etc/shadow'/>"
                          "<target dev='vdb' bus='virtio'/></disk>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("too many <disk>" in e for e in res.errors)

    def test_host_blockdev_disk_blocked(self, mdict, manifest):
        # Even the single disk must be file-backed, not a host block device.
        xml = _render_template(mdict).replace(
            "<disk type='file' device='disk'>",
            "<disk type='block' device='disk'>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("disk type='block'" in e for e in res.errors)

    def test_extra_channel_blocked(self, mdict, manifest):
        xml = _add_device(_render_template(mdict),
                          "<channel type='unix'><source mode='bind' "
                          "path='/run/evil.sock'/><target type='virtio' "
                          "name='org.qdistro.exfil'/></channel>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("channel" in e.lower() for e in res.errors)

    def test_qemu_commandline_namespace_bypass_blocked(self, mdict, manifest):
        # <qemu:commandline> can splice -netdev/-device args outside <devices>,
        # bypassing the per-device checks. Any namespaced element is rejected
        # (codex finding 2).
        xml = _render_template(mdict).replace(
            "<domain type='kvm'>",
            "<domain type='kvm' "
            "xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>")
        xml = xml.replace("</domain>",
                          "<qemu:commandline><qemu:arg value='-netdev'/>"
                          "<qemu:arg value='user,id=evil'/></qemu:commandline>"
                          "</domain>")
        res = validate_domain(manifest, xml)
        assert not res.ok
        assert any("namespaced element" in e for e in res.errors)


# ---------------------------------------------------------------------------
# USB attach allowlist (runtime wrapper boundary)
# ---------------------------------------------------------------------------
class TestUsbAttach:
    def test_allow_and_deny(self, mdict):
        mdict["usbHostdevAllow"] = [{"vendor": "0bda", "product": "8812"}]
        m = parse(mdict)
        assert validate_usb_attach(m, "0bda", "8812")
        assert validate_usb_attach(m, "0x0bda", "0x8812")     # 0x-prefixed
        assert not validate_usb_attach(m, "1d6b", "0002")     # not declared


# ---------------------------------------------------------------------------
# Schema rejection
# ---------------------------------------------------------------------------
class TestSchema:
    def test_bad_mgmt_mac_rejected(self, mdict):
        mdict["controlPlane"]["mgmtMac"] = "not-a-mac"
        assert not validate_manifest(mdict).ok
        with pytest.raises(MetadataSchemaError):
            parse(mdict)

    def test_bad_transport_rejected(self, mdict):
        mdict["controlPlane"]["transport"] = "carrier-pigeon"
        assert not validate_manifest(mdict).ok

    def test_bad_silo_prefix_rejected(self, mdict):
        mdict["interfaces"]["siloVifPrefix"] = "52:54:00"     # too short
        assert not validate_manifest(mdict).ok

    def test_duplicate_exposed_object_rejected(self, mdict):
        mdict["exposedServices"].append({"object": "system", "methods": ["x"]})
        assert not validate_manifest(mdict).ok

    def test_vsock_transport_allows_vsock_device(self, mdict):
        mdict["controlPlane"]["transport"] = "vsock"
        m = parse(mdict)
        xml = _add_device(_render_template(mdict),
                          "<vsock model='virtio'><cid auto='no' address='7'/></vsock>")
        assert validate_domain(m, xml).ok
