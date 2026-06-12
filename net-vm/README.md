# qdistro-netvm — OpenWrt net VM (task 4)

The OpenWrt VM that relocates the network hostile-input surface (802.11,
DHCP/DNS, firewall, tunnel termination) off the host and enforces per-silo
egress behind the stable silo-facing contract. Design source of truth:
`doc/networking.md`; implementation plan: `todo/fable-networking/`.

This directory is the **host-side** pipeline + the **image overlay**. The
per-silo egress *policy* is compiled to UCI by `session_manager/
qdistro_netvm_uci.py` (pure, golden-tested) and pushed to the VM by
`session_manager/qdistro_netvm_client.py` over rpcd JSON-RPC (Probe 2's
transport decision).

## Files

- `domain-template.xml` — libvirt XML. Headless, virtio, host-only mgmt vif +
  WAN uplink. Substituted by `install-netvm.sh`.
- `netvm-manifest.json` — **declarative source of truth** for what the VM may
  expose: control-plane endpoint, exposed ubus objects, per-silo vif MAC scheme,
  USB/PCI passthrough allowlists. The install guard refuses to define a domain
  that carries anything undeclared.
- `build-netvm-image.sh` — builds the OpenWrt image via the ImageBuilder (fixed
  release + explicit PACKAGES + the `image-files/` overlay). Emits a lineage
  sidecar (`*.manifest.txt`).
- `install-netvm.sh` — renders the domain, runs the manifest activation guard,
  then `virsh define`. **Fails closed** on any undeclared device.
- `qdistro-netvm-claim-nic.sh` — NetworkManager → net-VM NIC handoff (piece 5):
  NM releases the NIC, the VM claims it (PCI passthrough / USB redirect),
  recovery ethernet stays host-claimable. Every claim is checked against the
  manifest allowlist and audited.
- `image-files/` — the read-only overlay baked into the image:
  - `usr/libexec/rpcd/qdistro.netvm` — the control-plane ubus object
    (`egress_reload`, `wifi_join`); the host client's counterpart.
  - `usr/libexec/qdistro-netvm-apply` — regenerates `/etc/config/{network,
    firewall,dhcp}` = baseline + host-compiled per-silo overlay, then reloads.
  - `usr/share/rpcd/acl.d/qdistro-netvm.json` — the scoped admin ACL group.
  - `etc/qdistro-netvm/baseline/*` — baseline network/firewall/dhcp.
  - `etc/uci-defaults/99-qdistro-netvm` — first-boot: uhttpd `/ubus` on the
    mgmt vif only + the scoped rpcd login.

## Bringup recipe (host)

```sh
# 1. Build the OpenWrt base image (one-time; downloads the ImageBuilder).
./build-netvm-image.sh --dest /var/lib/libvirt/images/qdistro-netvm-base.qcow2

# 2. Create a host-only libvirt network for the mgmt/control-plane vif.
virsh net-define qdistro-netvm-mgmt.xml && virsh net-start qdistro-netvm-mgmt

# 3. Validate the rendered domain against the manifest (no define).
./install-netvm.sh --check

# 4. Define + start (the guard runs again before define).
./install-netvm.sh --start

# 5. Hand the real NIC to the VM once it's up (after NM releases it).
sudo ./qdistro-netvm-claim-nic.sh claim-pci 0000:03:00.0   # add to manifest first
```

## Control plane

The admin app / session manager speak rpcd JSON-RPC over HTTP to
`http://<mgmt-ip>/ubus`, bounded by the `qdistro-netvm-admin` ACL. Push a
compiled egress policy:

```python
from qdistro_silo_egress import EgressPolicy
from qdistro_netvm_uci import SiloNet, compile_all
from qdistro_netvm_client import NetVMClient

frags = compile_all([SiloNet(name="work", index=0,
                             policy=EgressPolicy.parse("direct"))])
c = NetVMClient(base_url="http://192.168.97.1/ubus", password=...)
c.egress_reload(frags)          # -> {"applied": True}
```

## Tests

- `tests/unit/test_netvm_uci.py` — the policy→UCI compiler (golden).
- `tests/unit/test_netvm_client.py` — the rpcd client vs a faithful fake rpcd.
- `tests/unit/test_netvm_manifest.py` — the activation guard (undeclared device
  blocks `virsh define`).
- `tests/unit/test_netvm_scripts.py` — script/overlay structure.
- `tests/integration/vm/s04-netvm-control-plane.sh` — the **end-to-end VM**
  test: real host client → live OpenWrt rpcd → `qdistro.netvm.egress_reload` →
  config lands + fw4 reloads. Verified 2026-06-12 against an OpenWrt 24.10.7
  rig (see `todo/fable-networking/VM-RUN-2026-06-12-task4.md`).

## Notes / known gaps

- The recovery-ethernet ownership rule is strict: normally down, admin-only,
  never forwarded to a silo, audited on enable, disabled when recovery ends.
- Per-silo *vif lifecycle* (hot-attaching a silo's vif as it starts, with a MAC
  under `siloVifPrefix`) is silo-start integration, separate from
  `egress_reload`, which only (re)applies policy.
- USB Wi-Fi dongle redirection + bridged throughput numbers still need physical
  hardware (Probe 2 steps 5 & 8).
