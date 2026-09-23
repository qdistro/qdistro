# 01 — Popup connects to stub native host

## Goal

Prove the extension loads, opens its native-messaging port, and the popup reflects "connected" once the stub host's first heartbeat lands. This is the end-to-end smoke for the whole repo — if it FAILs, nothing else is worth running.

## Preconditions

- `deploy.sh` has been run against `$VMNAME`. That installed Firefox, copied `dist/firefox/` and `stub-bridge.py` to `/home/admin/qdfirefox-extension/`, and wrote the native-host manifest.
- No Firefox is currently running in the VM. Setup will kill any stragglers.

## Setup

```bash
$VMEXEC $VM "pkill -u admin -f firefox || true; pkill -f stub-bridge.py || true"
$VMEXEC $VM "journalctl --rotate >/dev/null 2>&1 || true"
sleep 1
```

Launch Firefox as `admin` on `DISPLAY=:0`, fresh profile, with the unpacked extension auto-loaded. Two paths work; pick (a) unless `web-ext` is missing:

(a) **Using web-ext** (preferred — it auto-loads the unpacked extension):

```bash
$VMEXEC $VM "su - admin -c 'DISPLAY=:0 setsid web-ext run \\
    --source-dir /home/admin/qdfirefox-extension/dist/firefox \\
    --firefox /usr/bin/firefox \\
    --no-reload \\
    </dev/null >/tmp/firefox.log 2>&1 &'"
```

(b) **Fallback — manual about:debugging path** if `web-ext` is missing:

```bash
$VMEXEC $VM "zypper -n install -y nodejs-default || true"
$VMEXEC $VM "su - admin -c 'npm i -g web-ext'"
# then retry (a)
```

Wait 8 seconds for Firefox first-run + extension boot + stub-host first heartbeat.

```bash
sleep 8
```

## Steps

1. **screenshot popup-closed.png** — full-screen screenshot. The Firefox window should be open at `about:blank` (or first-run). The toolbar should show the qdistro extension's action button.

2. **Click the qdistro toolbar action.** OCR the toolbar for the qdistro icon's bounding box (the action has `default_title="qdistro browser bridge"`; hovering would surface that, but OCR-direct on the icon glyph is unreliable — fall back to clicking the rightmost custom-extension icon in the toolbar). Click it.

3. **screenshot popup-open.png** — the popup should be visible, ~320×220 px, containing the strings `qdistro browser bridge`, `Port:`, `connected` (in green) or `disconnected` (in red), `Ping`, `Export session`, `Container (Firefox)`.

4. **journal — proof that the stub host saw the port open and a heartbeat exchange occurred:**

```bash
$VMEXEC $VM "journalctl --identifier=qdistro-stub-bridge --since='1 minute ago' --no-pager"
```

## Assertions

- [ ] **Firefox launched.** `pgrep -u admin firefox` returns a PID. (`$VMEXEC $VM "pgrep -u admin firefox && echo OK"` → `OK`.)
- [ ] **Stub host started.** `$VMEXEC $VM "pgrep -af stub-bridge.py"` returns a process line; the parent is `firefox` or `plugin-container`.
- [ ] **Popup renders the expected layout.** OCR `popup-open.png` and confirm all of: "qdistro browser bridge", "Port:", "Ping", "Export session", "Container (Firefox)". A missing string is a FAIL.
- [ ] **Popup status is `connected`.** OCR `popup-open.png` — the word `connected` appears next to `Port:`, NOT `disconnected`. If `disconnected` appears, FAIL with the screenshot quoted.
- [ ] **Journal shows stub-bridge startup.** `journalctl --identifier=qdistro-stub-bridge ... --since='1 minute ago'` includes the line `stub-bridge starting`.
- [ ] **Journal shows at least one heartbeat sent + ack received.** Same journal output includes one or more `sent heartbeat seq=N` lines AND one or more `recv op=qdistro.heartbeat.ack` lines. Both directions must be present; missing either direction is a real bridge-contract bug, not a flake.

## Teardown

```bash
$VMEXEC $VM "pkill -u admin -f firefox || true"
$VMEXEC $VM "pkill -f stub-bridge.py || true"
$VMEXEC $VM "pkill -u admin -f web-ext || true"
```
