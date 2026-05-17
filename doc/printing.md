# Printing (CUPS in a VM)

## Motivation

CUPS has been the source of severe vulnerabilities — most recently the 2024
`cups-browsed` chain that allowed RCE via network-announced "printers." The
protocol surface (IPP, cups-browsed auto-discovery, proprietary print-driver
binaries) is broad, old, and not consistently audited.

qdistro treats CUPS as **untrusted** and isolates it in a **dedicated VM**.
Host apps print by sending jobs to a proxy that forwards into the CUPS VM.
Printer drivers, network interactions, and any CUPS-related exploit surface
stay inside the VM. Compromise is bounded to the VM filesystem, which is
rebuildable from image.

This is the same reasoning as the isolation ladder: the CUPS tier is tier 5
(VM + framed, though headless here).

## Architecture

```
 host (admin side) CUPS VM
 +----------------------------+ +--------------------+
 | user app | | cupsd |
 | | | | + printer drivers |
 | v | | + IPP server |
 | qdistro-print-proxy | +--------+-----------+
 | (Python system service) | |
 | | | |
 | v AF_VSOCK | |
 | +----+---------------------+-------------------+
 | | |
 +----------------------------+ |
 v
 network printer / USB printer
 (passed through to the VM)
```

## CUPS VM properties

- **Image**: minimal Tumbleweed (or Alpine) — just enough OS to run `cupsd`
 + drivers. The shipped image is Tumbleweed Minimal-VM Cloud with `cups`,
 `cups-filters`, `cups-browsed`, `cups-pdf`, and `socat` (vsock bridge
 from CID-any:631 to the local `cupsd` unix socket).
- **No user data.** Read-only OS image + tmpfs for the job spool. Nothing
 user-owned crosses the boundary.
- **No general network.** The virtio-net NIC is bridged to a **print-only
 VLAN** (if network printers exist) or only connected to the host via
 vsock (USB-only).
- **cups-browsed disabled.** No auto-discovery; no trusting of
 network-announced printers.
- **IPP server bound only to the host-side vsock endpoint.** Not reachable
 on the general network.
- **Read-only rootfs**, tmpfs for `/var/spool/cups`. The image is reset on
 every boot; no persistent state inside the VM.
- **Configuration** (printer list, drivers, defaults) lives in a separate
 subvolume mounted read-only except during admin-triggered reconfig.

The domain is headless: no video or sound channels; just `qemu-xhci` for USB
hot-plug, virtio-net (SLIRP) for cups-browsed outbound, vsock for IPP, and
`qemu-guest-agent` for host-driven job control.

## Print proxy on host

`qdistro-print-proxy.service` (Python, running as a dedicated `qdistro-print`
uid):

- Listens on a host-local IPP endpoint (`/run/qdistro-print/ipp.sock`).
- User apps and the GTK/Qt print dialogs see this as a normal CUPS daemon.
- For each incoming print job: authenticates the caller (peercred + polkit
 check), forwards the IPP request into the VM over vsock, relays
 responses.
- Enforces per-user policy: which user can print to which printer, job-size
 caps, page-count caps.
- Logs every job (user, timestamp, printer, page count, optionally MD5 of
 payload) to the admin audit log.

Apps don't know CUPS is in a VM. Standard IPP/CUPS client libraries just
work.

### Broker gate and spawn-on-demand

Before opening the backend, the proxy calls
`com.qdistro.AdminBroker1.CheckPermission(action, details)` with
`action=print.access` and details `{peer_uid, peer_pid, peer_exe}`. Gate
decisions: allow → forward, deny → close, unknown → forward + log
(default-allow during bring-up while admin authors `print.*` rules),
error → close (fail closed when the broker is required but unreachable).

If the VM backend is unreachable, the proxy invokes a spawn helper
(`spawn-print-vm.sh`), which runs `virsh start qdistro-print` and polls
boot via a vsock probe, then retries the connect.

## Printer drivers

- **Live entirely in the CUPS VM.** Never on the host.
- Proprietary drivers (HP PPDs, Canon binaries, etc.) run inside the VM;
 their blast radius is VM-bounded.
- Generic drivers (IPP Everywhere, driverless) are preferred when
 supported.

## Hardware access

### USB printers

- Admin identifies the printer's USB bus/device in the admin panel.
- `qdistro-print-attach-usb --vendor-product VVVV:PPPP` (or `--bus-addr
 BUS.ADDR`) attaches it to the CUPS VM via USB passthrough (qemu-xhci).
 Polkit-gated through `com.qdistro.print.attach-usb` (auth_admin
 defaults).
- No other user session or container can claim that USB device.
- If the printer is unplugged, the VM sees the disconnect; reconnection
 re-attaches automatically.

### Network printers

- Put network printers on a **separate VLAN** if network topology allows.
- The CUPS VM's NIC is bridged to that VLAN.
- Per-user netns are never routed to the print VLAN. A user's browser
 can't reach the printer directly; must go through the proxy.
- If a separate VLAN isn't available: at minimum, host-level firewall
 blocks everyone except the CUPS VM from reaching known printer IPs.

## Printer setup — no auto-discovery

`cups-browsed` is disabled. IPP Everywhere discovery is off for
user-triggered discovery.

Admin panel flow:

1. Admin clicks "Add printer."
2. Enters IP/hostname + model (or picks from an IPP Everywhere broadcast,
 admin-initiated, one-shot, not persistent listening).
3. The proxy forwards config into the CUPS VM.
4. The driver is installed inside the VM.
5. Test print.

No user-visible "nearby printers" UI. Users print to admin-configured
printers only.

## Per-user printing policy

polkit namespace `com.qdistro.print.*`:

- `com.qdistro.print.submit_job` — parameterized by user, printer, page
 count.
- Rules can gate by `(user, printer)`: "dev-user can print to office-bw,
 not to color-printer."
- Page-count / cost limits: prompt admin for approvals above N pages.
- Confidential docs: a per-app tag (via the SDK) can force admin approval
 regardless of defaults.

The polkit action library covers attach-usb / detach-usb / cancel-job /
purge-jobs / access.

## Management UI

The admin panel's "Printing" page:

- Configured printers (list, test, remove, set default).
- Active print jobs across users.
- Job history with audit details.
- CUPS VM status (running / stopped / health).
- "Reset CUPS VM" button — reboots the VM, clears any in-flight state.
 Safe hatch if something looks off.

Per-user launchers get a minimal view: their own recent jobs + status; no
configuration.

## Admin approval flow for print jobs

If policy requires approval for a job (large, confidential, new printer),
polkit prompts via the usual admin agent on tty3 (or the phone). Admin
approves → the proxy releases the job into the VM.

## Per-job audit

Every print operation is logged via `qdistro_print_audit.py` (SQLite at
`/var/lib/qdistro/audit/print_audit.sqlite`). Rows capture op, decision,
reason, caller uid/pid/exe, and backend.

## Why a VM and not a container

- CUPS vulnerabilities have included kernel-adjacent exploit chains (via
 printer-driver binaries and cups-browsed's protocol parsing). A
 container shares the host kernel; a VM doesn't.
- Printer drivers are often proprietary C code with limited review.
 Sandboxing inside a VM is a much stronger boundary than user namespaces.
- VM overhead (memory, disk) is modest for a headless Tumbleweed/Alpine.

Container isolation is the right tool elsewhere; for CUPS specifically,
VM is correct.

## No fallback to host CUPS

If the CUPS VM is down or misbehaving, printing fails with a clear error.
**No automatic fallback to host-level CUPS** — the whole point is that
the host never runs CUPS. Admin restarts or rebuilds the VM from image.

## Driver updates

Drivers live inside the VM; updating them means rebuilding the VM image
(or updating in place via the VM's own package manager). Standard
image-refresh cadence — probably weekly alongside OS updates. Snapper on
`/var/lib/libvirt/images` preserves previous VM image versions, so bad
driver updates can be rolled back.

## Scanner support

The same model if scanners are in scope: `sane-airscan` (eSCL) over an
IPP-like protocol is the modern driverless path; could live in the same
VM or a parallel "scan VM."
