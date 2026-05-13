# qdistro-print-proxy — + (spec/20 CUPS-in-VM)

Host-side IPP proxy that lets user apps print without ever invoking a
host CUPS daemon. ships the transport; adds the
broker gate + spawn-on-demand. The CUPS VM image build itself is
deferred to .

## Architecture (current state)

```
 user app
 │ CUPS_SERVER=/run/qdistro-print/ipp.sock
 ▼
 qdistro-print-proxy (this)
 │ AF_VSOCK | AF_UNIX | AF_INET (selectable via env)
 ▼
 <backend> ← currently undeployed; tests wire AF_UNIX
 to a stub. will wire AF_VSOCK to
 a per-host CUPS VM cupsd bound on vsock:631.
```

## Backend selection

| Env var | Meaning | Default |
|--------------------------------|----------------------------------------------|------------------|
| `QDISTRO_PRINT_BACKEND` | `vsock` / `unix` / `tcp` | `vsock` |
| `QDISTRO_PRINT_VSOCK_CID` | remote VM CID | `3` |
| `QDISTRO_PRINT_VSOCK_PORT` | remote IPP port | `631` |
| `QDISTRO_PRINT_UNIX_PATH` | backend AF_UNIX path | `/run/cups/cups.sock` |
| `QDISTRO_PRINT_TCP_HOST` | backend TCP host | `127.0.0.1` |
| `QDISTRO_PRINT_TCP_PORT` | backend TCP port | `631` |
| `QDISTRO_PRINT_LISTEN` | frontend AF_UNIX listen path | `/run/qdistro-print/ipp.sock` |

## : gate + spawn-on-demand

| Env var | Meaning | Default |
|----------------------------------|--------------------------------------------------|------------------|
| `QDISTRO_PRINT_GATE_REQUIRED` | `0` / `1` — call broker.CheckPermission per conn | `0` (off) |
| `QDISTRO_PRINT_GATE_ACTION` | broker action name | `print.access` |
| `QDISTRO_PRINT_VM_SPAWN` | path to spawn helper, or empty | empty (off) |
| `QDISTRO_PRINT_SPAWN_BACKOFF_S` | seconds to wait after spawn before retry | `1.5` |

**Gate decisions:**

| Broker verdict | Proxy action |
|----------------|-----------------------------------------|
| `allow` | forward |
| `deny` | close |
| `unknown` | forward + log (default-allow MVP) |
| error / down | close (fail closed when gate required) |

**Spawn-on-demand:** when the vsock backend is unreachable AND the
spawn helper is set, the proxy invokes it once + retries the connect
after a short backoff. The reference helper `spawn-print-vm.sh` runs
`virsh start qdistro-print` and polls boot via vsock probe; SKIPs
cleanly when the libvirt domain doesn't exist (so this can ship before
the actual VM image).

## What's NOT in (deferred to +)

- The CUPS VM image build itself (libvirt domain `qdistro-print`,
 tier-5-style Alpine or MicroOS base with cupsd bound on vsock:631).
- Job-size + page-count caps.
- Job audit recording (broker `pwd_audit`-style row).
- USB printer hot-plug → libvirt `attach-device`.
- Admin panel "Printing" page.
- `print.*` rule library (broker authoring + admin UI).

The deferred list maps directly to spec/20 §"Print proxy on host" +
§"Per-user printing policy" + §"Hardware access".

## Tests

- `tests/unit/test_print_proxy.py` — 3 cases (byte forwarding via
 unix backend round-trip, listener mode = 0660, backend-unreachable
 → fast-close).
- `tests/unit/test_print_proxy_gate.py` — 7 cases (gate disabled
 by default, gate required + broker unreachable → fail closed,
 spawn helper invoked on vsock ECONNREFUSED, spawn helper skipped
 for non-vsock, _gate function direct, spawn helper handles missing
 virsh, spawn helper SKIPs when domain missing).
