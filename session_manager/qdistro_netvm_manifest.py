"""Net-VM domain manifest + fail-closed activation guard (task 4 piece 2).

The net VM is the OpenWrt exception to the NixOS guest-definition rule
(``doc/networking.md`` §"VM definition exception"), but it still obeys the
resource-model contract (``doc/resources.md``, ``doc/vm-definitions.md``): the
*manifest* is the declarative source of truth for what the VM may expose, and
**activation fails closed on any device or service the manifest does not
declare** (``doc/vm-definitions.md`` §"Runtime Policy": an image exposing an
undeclared service "should block image activation").

For a network-hostile-surface VM this guard is load-bearing, not bureaucracy:
the whole point is that the net VM holds the 802.11/DHCP/DNS/firewall attack
surface, so a device that slips into its libvirt domain undeclared — an extra
passthrough NIC, a stray vsock channel, a graphics framebuffer — is exactly the
silent privilege the manifest exists to forbid. This module is **pure**: it
parses the manifest + the rendered libvirt XML and returns violations, with no
I/O, so the activation gate is unit-testable headless (the install script calls
:func:`assert_activatable` before ``virsh define``).

What the manifest declares (and the guard cross-checks against the domain XML):

  * **control plane** — the rpcd-JSON-RPC-over-HTTP endpoint on the host-only
    management vif (Probe 2's transport decision) and its scoped ACL group.
  * **exposed services** — the ubus objects/methods the scoped ACL grants; the
    host-side client (:mod:`qdistro_netvm_client`) is bounded by this set.
  * **interfaces** — the mgmt vif, the WAN uplink, the recovery-ethernet escape
    hatch, and the per-silo vif MAC scheme. Any ``<interface>`` MAC in the XML
    not matching one of these is undeclared ⇒ activation blocked.
  * **usbHostdevAllow / pciPassthrough** — the only USB Wi-Fi dongles / PCI NIC
    the VM may claim (the hostile-hardware boundary). Any ``<hostdev>`` not on
    the allowlist is undeclared ⇒ blocked. The runtime attach wrapper reuses
    :func:`validate_usb_attach` as its polkit-gated allowlist, mirroring
    ``print-vm``'s ``qdistro-print-allowlist``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from qdistro_metadata_schema import MetadataSchemaError, ValidationResult

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
# Libvirt device elements that are part of every headless VM and need no
# per-device manifest declaration (the baseline substrate, not exposed surface).
_BASELINE_DEVICES = frozenset({
    "emulator", "controller", "serial", "console", "channel", "memballoon",
    "rng", "disk", "watchdog", "input",
})
# Per-tag multiplicity caps: these singleton/limited devices appear at most this
# many times in the baseline domain. More than the cap is undeclared surface
# even though the tag is "baseline" — a second <disk> or <channel> carries a
# host file/socket. controllers (pcie-root + virtio-serial) and auto-added
# inputs get a small allowance.
_BASELINE_MAX = {
    "emulator": 1, "serial": 1, "console": 1, "channel": 1,
    "memballoon": 1, "rng": 1, "disk": 1, "watchdog": 1,
    "controller": 4, "input": 4,
}


def _norm_mac(mac: Any) -> str | None:
    if not isinstance(mac, str):
        return None
    m = mac.strip().lower()
    return m if _MAC_RE.match(m) else None


@dataclass(frozen=True)
class NetVMManifest:
    """The parsed, validated net-VM manifest. Construct via :func:`parse`."""

    name: str
    transport: str
    endpoint: str
    mgmt_mac: str
    wan_mac: str | None
    recovery_mac: str | None
    silo_vif_prefix: str | None          # e.g. "52:54:00:9c:01:" (5 octets)
    max_silo_vifs: int
    exposed: tuple[tuple[str, frozenset[str]], ...]   # (object, methods)
    usb_allow: frozenset[tuple[str, str]]             # (vendor, product) lower
    pci_allow: frozenset[str]                         # domain:bus:slot.func
    allow_vsock: bool
    allow_graphics: bool

    # ---- declared-MAC predicate ------------------------------------------
    def declares_mac(self, mac: str) -> bool:
        m = _norm_mac(mac)
        if m is None:
            return False
        if m in {self.mgmt_mac, self.wan_mac, self.recovery_mac}:
            return True
        if self.silo_vif_prefix and m.startswith(self.silo_vif_prefix):
            # Last octet must be a valid slot within the declared budget.
            try:
                slot = int(m.rsplit(":", 1)[1], 16)
            except ValueError:
                return False
            return 0 <= slot < self.max_silo_vifs
        return False

    def exposes(self, obj: str, method: str) -> bool:
        for o, methods in self.exposed:
            if o == obj and ("*" in methods or method in methods):
                return True
        return False


# --------------------------------------------------------------------------
# Manifest parsing / schema validation
# --------------------------------------------------------------------------
def validate_manifest(obj: Any) -> ValidationResult:
    """Validate the manifest dict's shape; returns accumulated errors."""
    res = ValidationResult()
    if not isinstance(obj, dict):
        res.errors.append(f"manifest must be an object, got {type(obj).__name__}")
        return res

    name = obj.get("name")
    if not isinstance(name, str) or not name:
        res.errors.append("manifest.name must be a non-empty string")

    cp = obj.get("controlPlane")
    if not isinstance(cp, dict):
        res.errors.append("manifest.controlPlane must be an object")
        cp = {}
    transport = cp.get("transport")
    if transport not in ("rpcd-http", "vsock"):
        res.errors.append(
            "controlPlane.transport must be 'rpcd-http' or 'vsock', got "
            f"{transport!r}")
    if not isinstance(cp.get("endpoint"), str) or not cp.get("endpoint"):
        res.errors.append("controlPlane.endpoint must be a non-empty string")
    if _norm_mac(cp.get("mgmtMac")) is None:
        res.errors.append(
            f"controlPlane.mgmtMac is not a valid MAC: {cp.get('mgmtMac')!r}")

    ifaces = obj.get("interfaces")
    if not isinstance(ifaces, dict):
        res.errors.append("manifest.interfaces must be an object")
        ifaces = {}
    for key in ("wanMac", "recoveryMac"):
        v = ifaces.get(key)
        if v is not None and _norm_mac(v) is None:
            res.errors.append(f"interfaces.{key} is not a valid MAC: {v!r}")
    prefix = ifaces.get("siloVifPrefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not re.match(
                r"^([0-9a-fA-F]{2}:){5}$", prefix):
            res.errors.append(
                "interfaces.siloVifPrefix must be 5 hex octets ending in ':' "
                f"(e.g. '52:54:00:9c:01:'), got {prefix!r}")
    max_vifs = ifaces.get("maxSiloVifs", 0)
    if not isinstance(max_vifs, int) or isinstance(max_vifs, bool) \
            or not (0 <= max_vifs <= 256):
        res.errors.append(
            f"interfaces.maxSiloVifs must be an int in 0..256, got {max_vifs!r}")

    res.merge(_validate_exposed(obj.get("exposedServices")))
    res.merge(_validate_hostdev_allow(obj.get("usbHostdevAllow"),
                                      obj.get("pciPassthrough")))
    return res


def _validate_exposed(services: Any) -> ValidationResult:
    res = ValidationResult()
    if services is None:
        return res
    if not isinstance(services, list):
        res.errors.append("exposedServices must be a list")
        return res
    seen: set[str] = set()
    for i, e in enumerate(services):
        if not isinstance(e, dict):
            res.errors.append(f"exposedServices[{i}] must be an object")
            continue
        obj = e.get("object")
        if not isinstance(obj, str) or not obj:
            res.errors.append(f"exposedServices[{i}].object must be a string")
            continue
        if obj in seen:
            res.errors.append(f"exposedServices: duplicate object {obj!r}")
        seen.add(obj)
        methods = e.get("methods")
        if not isinstance(methods, list) or not methods or not all(
                isinstance(m, str) for m in methods):
            res.errors.append(
                f"exposedServices[{i}].methods must be a non-empty string list")
    return res


def _validate_hostdev_allow(usb: Any, pci: Any) -> ValidationResult:
    res = ValidationResult()
    if usb is not None:
        if not isinstance(usb, list):
            res.errors.append("usbHostdevAllow must be a list")
        else:
            for i, e in enumerate(usb):
                if not isinstance(e, dict) or not re.match(
                        r"^[0-9a-fA-F]{4}$", str(e.get("vendor", ""))) \
                        or not re.match(
                        r"^[0-9a-fA-F]{4}$", str(e.get("product", ""))):
                    res.errors.append(
                        f"usbHostdevAllow[{i}] needs 4-hex vendor+product")
    if pci is not None:
        if not isinstance(pci, list):
            res.errors.append("pciPassthrough must be a list")
        else:
            for i, e in enumerate(pci):
                addr = e.get("address") if isinstance(e, dict) else None
                if not isinstance(addr, str) or not re.match(
                        r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:"
                        r"[0-9a-fA-F]{2}\.[0-9a-fA-F]$", addr):
                    res.errors.append(
                        f"pciPassthrough[{i}].address must be "
                        "DDDD:BB:SS.F PCI form")
    return res


def parse(obj: Any) -> NetVMManifest:
    """Validate + build a :class:`NetVMManifest`. Raises on schema error."""
    res = validate_manifest(obj)
    if not res.ok:
        raise MetadataSchemaError("; ".join(res.errors))
    cp = obj["controlPlane"]
    ifaces = obj["interfaces"]
    exposed = tuple(
        (e["object"], frozenset(e["methods"]))
        for e in (obj.get("exposedServices") or []))
    usb = frozenset(
        (str(e["vendor"]).lower(), str(e["product"]).lower())
        for e in (obj.get("usbHostdevAllow") or []))
    pci = frozenset(
        e["address"].lower() for e in (obj.get("pciPassthrough") or []))
    prefix = ifaces.get("siloVifPrefix")
    return NetVMManifest(
        name=obj["name"],
        transport=cp["transport"],
        endpoint=cp["endpoint"],
        mgmt_mac=_norm_mac(cp["mgmtMac"]),       # type: ignore[arg-type]
        wan_mac=_norm_mac(ifaces.get("wanMac")),
        recovery_mac=_norm_mac(ifaces.get("recoveryMac")),
        silo_vif_prefix=prefix.lower() if prefix else None,
        max_silo_vifs=int(ifaces.get("maxSiloVifs", 0)),
        exposed=exposed,
        usb_allow=usb,
        pci_allow=pci,
        allow_vsock=cp["transport"] == "vsock",
        allow_graphics=bool(obj.get("graphics", False)),
    )


# --------------------------------------------------------------------------
# Domain-XML activation guard (the fail-closed core)
# --------------------------------------------------------------------------
def validate_domain(manifest: NetVMManifest, xml_text: str) -> ValidationResult:
    """Cross-check a rendered libvirt domain against the manifest.

    Every device the domain exposes must be declared. Returns a
    :class:`ValidationResult`; ``ok`` means safe to ``virsh define``."""
    res = ValidationResult()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        res.errors.append(f"domain XML is not well-formed: {e}")
        return res
    # Reject ANY namespaced extension element anywhere in the domain — most
    # importantly <qemu:commandline>, which can splice in netdevs/-device args
    # that never appear under <devices> and would bypass the per-device checks
    # below entirely. ElementTree renders a namespaced tag as "{uri}local"; our
    # template uses none, so any "{...}" tag is an undeclared escape hatch.
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.startswith("{"):
            res.errors.append(
                f"undeclared namespaced element <{el.tag}>: qemu/extension "
                "overrides can add devices outside <devices>")
    devices = root.find("devices")
    if devices is None:
        res.errors.append("domain has no <devices> section")
        return res

    # Count baseline-tag devices: each is allowed only in its expected small
    # multiplicity. A second <disk> (host file/blockdev), an extra <channel>
    # (host socket), or a duplicate controller is NOT baseline — tag membership
    # alone is not enough (codex finding 1).
    counts: dict[str, int] = {}
    for dev in list(devices):
        tag = dev.tag
        counts[tag] = counts.get(tag, 0) + 1
        if tag == "interface":
            mac_el = dev.find("mac")
            mac = mac_el.get("address") if mac_el is not None else None
            if mac is None or not manifest.declares_mac(mac):
                res.errors.append(
                    f"undeclared <interface> MAC {mac!r}: not the mgmt/wan/"
                    "recovery vif nor a declared per-silo vif")
        elif tag == "hostdev":
            res.merge(_check_hostdev(manifest, dev))
        elif tag == "vsock":
            if not manifest.allow_vsock:
                res.errors.append(
                    "undeclared <vsock>: control-plane transport is "
                    f"{manifest.transport!r}, vsock not declared")
        elif tag == "graphics":
            if not manifest.allow_graphics:
                res.errors.append(
                    "undeclared <graphics>: the net VM is headless "
                    "(manifest.graphics is false)")
        elif tag == "video":
            if not manifest.allow_graphics:
                res.errors.append("undeclared <video>: net VM is headless")
        elif tag == "disk":
            res.merge(_check_disk(dev))
        elif tag == "channel":
            res.merge(_check_channel(dev))
        elif tag in _BASELINE_DEVICES:
            continue
        else:
            res.errors.append(f"undeclared device <{tag}>")

    # Multiplicity caps on the singleton baseline devices.
    for tag, cap in _BASELINE_MAX.items():
        if counts.get(tag, 0) > cap:
            res.errors.append(
                f"too many <{tag}> ({counts[tag]} > {cap}): only the baseline "
                "instance is declared")
    return res


def _check_disk(dev: ET.Element) -> ValidationResult:
    """A baseline disk must be a file-backed qcow2 (the OpenWrt image clone),
    never a host block device / passthrough (`type='block'`/`dev='…'`)."""
    res = ValidationResult()
    if dev.get("device", "disk") != "disk":
        res.errors.append(
            f"undeclared disk device='{dev.get('device')}' (expected 'disk')")
    if dev.get("type") not in (None, "file"):
        res.errors.append(
            f"undeclared disk type='{dev.get('type')}': only a file-backed "
            "image is allowed, not a host block/dir/network disk")
    return res


def _check_channel(dev: ET.Element) -> ValidationResult:
    """The only baseline channel is the qemu guest agent."""
    res = ValidationResult()
    tgt = dev.find("target")
    name = tgt.get("name") if tgt is not None else None
    if name != "org.qemu.guest_agent.0":
        res.errors.append(
            f"undeclared <channel> target {name!r}: only the guest agent "
            "channel is baseline")
    return res


def _check_hostdev(manifest: NetVMManifest, dev: ET.Element) -> ValidationResult:
    res = ValidationResult()
    subsys = dev.get("type")
    src = dev.find("source")
    if subsys == "usb":
        vp = src.find("vendor") if src is not None else None
        pp = src.find("product") if src is not None else None
        vendor = (vp.get("id") if vp is not None else "") or ""
        product = (pp.get("id") if pp is not None else "") or ""
        # libvirt ids are 0x-prefixed hex; normalise to bare 4-hex.
        key = (vendor.lower().removeprefix("0x"),
               product.lower().removeprefix("0x"))
        if key not in manifest.usb_allow:
            res.errors.append(
                f"undeclared USB hostdev {key[0]}:{key[1]}: not in "
                "manifest.usbHostdevAllow")
    elif subsys == "pci":
        addr = src.find("address") if src is not None else None
        if addr is None:
            res.errors.append("PCI hostdev has no <address>")
        else:
            dbsf = "{:04x}:{:02x}:{:02x}.{:x}".format(
                int(addr.get("domain", "0x0"), 16),
                int(addr.get("bus", "0x0"), 16),
                int(addr.get("slot", "0x0"), 16),
                int(addr.get("function", "0x0"), 16))
            if dbsf not in manifest.pci_allow:
                res.errors.append(
                    f"undeclared PCI hostdev {dbsf}: not in "
                    "manifest.pciPassthrough")
    else:
        res.errors.append(f"unsupported hostdev type {subsys!r}")
    return res


def assert_activatable(manifest: NetVMManifest, xml_text: str) -> None:
    """Fail-closed gate: raise :class:`MetadataSchemaError` if the domain
    exposes anything the manifest does not declare. The install script calls
    this immediately before ``virsh define``."""
    res = validate_domain(manifest, xml_text)
    if not res.ok:
        raise MetadataSchemaError(
            "net-VM activation blocked: " + "; ".join(res.errors))


def validate_usb_attach(manifest: NetVMManifest, vendor: str,
                        product: str) -> bool:
    """Allowlist check for a runtime USB attach (the polkit-gated wrapper's
    boundary). Returns True iff the (vendor, product) is declared."""
    v = str(vendor).lower().removeprefix("0x")
    p = str(product).lower().removeprefix("0x")
    return (v, p) in manifest.usb_allow


# --------------------------------------------------------------------------
# CLI: the install script's pre-`virsh define` gate
# --------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    """``qdistro_netvm_manifest <manifest.json> <domain.xml>`` — exit 0 if the
    rendered domain is activatable under the manifest, non-zero (printing the
    violations) otherwise. install-netvm.sh calls this before defining."""
    import json
    import sys
    if len(argv) != 2:
        print("usage: qdistro_netvm_manifest <manifest.json> <domain.xml>",
              file=sys.stderr)
        return 2
    manifest_path, xml_path = argv
    try:
        manifest = parse(json.loads(open(manifest_path).read()))
    except (OSError, ValueError, MetadataSchemaError) as e:
        print(f"manifest invalid: {e}", file=sys.stderr)
        return 3
    try:
        xml_text = open(xml_path).read()
    except OSError as e:
        print(f"cannot read domain XML: {e}", file=sys.stderr)
        return 3
    res = validate_domain(manifest, xml_text)
    if res.ok:
        print("net-VM domain is activatable: all devices declared")
        return 0
    for v in res.errors:
        print(f"BLOCKED: {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
