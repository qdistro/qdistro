# Firefox GUI tests — runner notes

Same shape as `qdistro/tests/integration/permissions-gui/AGENTS.md`. Read that file first if you haven't — most pitfalls (vm-exec quoting, modifier-key chords via `virsh send-key`, OCR-based click targeting) apply unchanged.

## What's specific to this corpus

- **Target app**: `firefox` in the VM, launched as `admin` on `DISPLAY=:0`.
- **Extension under test**: `qdfirefox-extension`. The host repo at `/home/playai/doc/qdistro-org/qdfirefox-extension/` is the source of truth; the VM gets a pre-built `dist/firefox/` tree copied in by Setup.
- **Native host**: `stub-bridge.py` in this directory. It speaks Firefox's native-messaging framing, sends a `qdistro.heartbeat` every 5s, and replies to `qdistro.ping` with `qdistro.ping.reply`. Journal-logs every event under `journalctl --identifier=qdistro-stub-bridge`.
- **The load-bearing assertion is journal lines, not pixels.** Per the qdistro test-harness rule, scenarios assert on `journalctl -t qdistro-stub-bridge` output to prove the wire protocol worked end-to-end. Screenshots are corroborating evidence — they catch popup-render regressions and OCR the visible status text, but the journal proves the port connected and the right ops were exchanged.

## Setup outline (deploy.sh below codifies it)

```bash
# 1. Install Firefox.
vm-exec $VM "zypper -n install MozillaFirefox"

# 2. Build the extension on the host (npm run build).
( cd /home/playai/doc/qdistro-org/qdfirefox-extension && npm run build )

# 3. Copy dist/firefox/ and stub-bridge.py into the VM under /home/admin/.
#    Use a tar pipe through vm-exec; virsh has no native scp.

# 4. Install the native-host manifest at
#    /home/admin/.mozilla/native-messaging-hosts/qdistro.json
#    pointing path → the stub-bridge.py copy.
#    allowed_extensions = ["qdistro-firefox@qdistro.local"].

# 5. Launch firefox with --remote-debugging-port=0 and load the extension
#    via about:debugging "Load Temporary Add-on".
```

The `deploy.sh` script in this directory automates 1–5. Scenarios reference it instead of repeating the boilerplate.

## Why the stub host

The real `qdistro-browser-bridge` daemon (in `qdistro/`) is not yet built for VM deployment. The stub matches the wire protocol the extension expects but has zero policy logic — it just acks heartbeats and echoes pings. That's enough to:

- Prove `connectNative` succeeds (port opens, status flips to "connected").
- Prove heartbeat/heartbeat.ack round-trips.
- Prove ping → ping.reply with the right `request_id` correlation.
- Prove the `cookies.export` / `containers.list` paths reach the host.

It does NOT validate policy decisions, intent-token HMAC, or audit logging — those live in the real bridge and the qdistro broker.

## Teardown

```bash
pkill -u admin -f firefox || true
pkill -f stub-bridge.py || true
journalctl --rotate >/dev/null 2>&1  # keep journals trim between scenarios
```

The native-host manifest and the dist tree are left in place — re-deploying is a no-op for unchanged files.
