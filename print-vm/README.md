# spec/20 — qdistro-print VM

The qdistro-print libvirt domain that hosts cupsd. The host-side
print proxy (`qdistro-print-proxy`) talks to it over AF_VSOCK.

## Files

- `domain-template.xml` — libvirt XML; placeholders substituted by `install-print-vm.sh`.
- `build-print-image.sh` — one-time host-side image build (Tumbleweed Minimal-VM + cups + socat vsock bridge).
- `install-print-vm.sh` — define + autostart the libvirt domain.
- `qdistro-print-attach-usb.sh` — polkit-gated `virsh attach-device` for USB printers.
- `qdistro-print-detach-usb.sh` — polkit-gated `virsh detach-device`.

## Bringup recipe (host)

```sh
# 1. Build the base image (one-time, ~10 min on a cold cache).
sudo ./build-print-image.sh

# 2. Define + autostart the qdistro-print domain.
sudo ./install-print-vm.sh --start

# 3. Verify the host proxy can reach the VM via vsock.
QDISTRO_PRINT_BACKEND=vsock QDISTRO_PRINT_VSOCK_CID=4 \
QDISTRO_PRINT_VSOCK_PORT=631 \
 /usr/local/bin/qdistro-print-proxy &

# 4. Print from any app:
CUPS_SERVER=/run/qdistro-print/ipp.sock lp -d <printer> file.pdf
```

## USB printer attach

```sh
qdistro-print-attach-usb --vendor-product 0411:00be # via lsusb VID:PID
qdistro-print-attach-usb --bus-addr 1.2 # via lsusb -d 1:2
```

Both wrappers gate through polkit (`org.qdistro.print.{attach,detach}-usb`).
The qdistro-polkit-agent renders the prompt; default config maps
`org.qdistro.print.*` to `broker` so admin sees the request in the
admin-approval-app queue.

Attach is pinned to the `qdistro-print` libvirt domain and checks
`/etc/qdistro/printvm-manifest.json` before calling `virsh`. Add known USB
printer IDs under `usbHostdevAllow`, for example:

```json
{ "name": "qdistro-print", "usbHostdevAllow": ["0411:00be"] }
```

An empty or missing manifest refuses every USB attach.

## CID

Default vsock CID is `4`. If you change it (`QDISTRO_PRINT_VM_CID=5
./install-print-vm.sh --force`), also flip `QDISTRO_PRINT_VSOCK_CID=5`
on the proxy side.

## Audit

Every proxy connection records a `(connect, allow|deny|error)` row
in `/var/lib/qdistro/audit/print_audit.sqlite`. The IPP byte payloads
themselves are NEVER persisted. Override the DB path via
`QDISTRO_PRINT_AUDIT_DB`.
