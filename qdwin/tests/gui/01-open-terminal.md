# 01 — open a terminal via the qdshell launcher

<!-- qci:visual: required -->

**Acceptance criterion:** an admin user can open a terminal on the
qdwin desktop via the launcher overlay and see a usable shell prompt.
This is the smoke test for "is the desktop session usable at all".

## Setup

```bash
source phase1/gui-tests/qdwin/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

# Host HTTP server on :8765 (qdwin-helpers' ctrl-socket path uses it).
pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1

# Session must be up under admin with the ctrl-socket reachable.
qdwin_session_healthy || { echo "FAIL: qdshell ctrl-socket not reachable"; exit 1; }

# Locker pre-check: B1 (locker stuck after unlock) means the screen
# can show a lock surface even when the internal state says
# locked=False. If we don't clear it the scenario asserts fire on the
# wrong pixels. Drain by restarting greetd-qdwin if needed.
LOCKER_STATE=$(qdwin_ctrl "locker")
case "$LOCKER_STATE" in
    *"attached=yes"*)
        # Either really locked or stale-attached — restart for a clean slate.
        cat > ${QDWIN_REPO}/extra/clean-locker.sh <<'EOF'
systemctl restart greetd-qdwin.service
for i in $(seq 1 30); do
    [ -S /run/user/1000/qdshell.sock ] && break
    sleep 0.5
done
sleep 2
EOF
        "$QDWIN_VM_EXEC" "$VMNAME" 'wget -qO /tmp/cl.sh http://10.0.2.2:8765/extra/clean-locker.sh && bash /tmp/cl.sh' >/dev/null
        qdwin_session_healthy || { echo "FAIL: session not back after locker reset"; exit 1; }
        ;;
esac
```

## Steps

### Step 1 — baseline screenshot

```bash
qdwin_screenshot /tmp/01-step1-baseline.png
```

**Assert (1.1):** the screenshot shows the qdshell panel along the
bottom edge: a hamburger ≡ glyph on the left, a clock `HH:MM` on the
right. The rest of the screen is the dark background. If panel
labels look like empty rectangles (tofu glyphs), the VM is missing
font packages — fail with `INFRA: fonts not installed`.

### Step 2 — open the launcher via Ctrl+Space

`qdwin_send_key` sends each key as a separate press+release in
sequence. For chords (modifier+key held simultaneously) use
`qdwin_chord` — the compositor's Ctrl+Space key binding only fires
when both keys are held at the same instant.

```bash
qdwin_chord ctrl -- spc
sleep 0.5
qdwin_screenshot /tmp/01-step2-launcher.png
qdwin_ctrl "launcher"
```

**Assert (2.1):** the launcher overlay is visible — a centred dark
rectangle ~480×360 px with a `>` prompt at the top and one or more
menu rows below.
**Assert (2.2):** `qdwin_ctrl "launcher"` response contains
`visible=True` and `indexed>=1`. `indexed=0` means the launcher
couldn't find any `.desktop` entries — fail with
`INFRA: launcher index empty`.

### Step 3 — filter to "foot" and confirm a match

```bash
qdwin_ctrl "launcher-type foot"
qdwin_screenshot /tmp/01-step3-filtered.png
```

**Assert (3.1):** ctrl response contains `matches>=1`. If
`matches=0`, `foot.desktop` is missing — usually because foot was
installed AFTER the session started (the launcher index doesn't
rescan). Fail with `INFRA: foot.desktop not in launcher index`.

### Step 4 — activate the selected entry

```bash
qdwin_ctrl "launcher-activate"
sleep 2
qdwin_screenshot /tmp/01-step4-activated.png
qdwin_ctrl "list"
```

**Assert (4.1):** ctrl response from `launcher-activate` is
`ok spawned <Name>` (not `err empty` and not `err spawn ...`).
**Assert (4.2):** `qdwin_ctrl "list"` lists at least one toplevel
(format `tl <handle> <uid> <wxh> <title>`). Title field is often
empty (`''`) on first cycle — qdshell only updates it when foot sends
a `set_title` request, which can lag the initial map. The chrome
titlebar in the screenshot is the authoritative app-id signal.
**Assert (4.3):** the screenshot shows a terminal window — dark
background, monospace text (at minimum a shell prompt like `admin@…$ `
or similar). If only a thin chrome strip with a red close button is
visible (no terminal content), this is the **CSD vs SSD chrome
collision** for foot — record FAIL with `BUG: foot CSD double-decorates`
and capture the screenshot.

### Step 5 — type into the terminal

Use `qdwin_type_lower` (sends one key per virsh call) — passing
multiple `KEY_*` args to `qdwin_send_key` makes virsh fire them as a
*chord* (all pressed simultaneously), which produces only one
character at the foot prompt. `qdwin_type_lower` exists exactly to
avoid this.

```bash
qdwin_type_lower "echo hello"
qdwin_send_key KEY_ENTER
# 1.0s, not 0.5s — qdwin_type_lower is ~40ms/key * 10 chars = ~400ms,
# plus shell echo + Enter handling. A 0.5s window catches the screen
# mid-type ("echo he"); 1.0s waits for the round-trip to settle.
sleep 1.0
qdwin_screenshot /tmp/01-step5-typed.png
```

**Assert (5.1):** the screenshot shows the typed command `echo hello`
on one line and the output `hello` on the next. Both should be in
the terminal's monospace font; the prompt should advance to a fresh
line below.

## Cleanup

```bash
# Close the launcher if still open.
state=$(qdwin_ctrl "launcher")
case "$state" in *visible=True*) qdwin_send_key KEY_ESC ;; esac

# Kill the foot we spawned.
cat > ${QDWIN_REPO}/extra/cleanup-foot.sh <<'EOF'
pkill -u admin -x foot 2>/dev/null || true
EOF
"$QDWIN_VM_EXEC" "$VMNAME" 'wget -qO /tmp/cf.sh http://10.0.2.2:8765/extra/cleanup-foot.sh && bash /tmp/cf.sh'
```

## Pass criteria

- All steps 1–5 produce the expected screenshots.
- `list` snapshot at step 4 lists the foot toplevel.
- `echo hello` round-trip in step 5 visible in the screenshot.

## Known failure modes (current gaps to file as bugs, not test bugs)

1. **`BUG: foot CSD double-decorates`** — qdshell attaches SSD chrome
   while foot draws CSD. Toplevel renders at foot's tiny default
   geometry with stacked decorations. Reproduces every run as of
   2026-04-30 on `qdistro-dev-260421-1957`-cloned VMs. Fix path:
   either advertise xdg-decoration-v1 with `mode=server-side` (per
   spec/27 was declined — revisit) or constrain the launcher to
   spawn only SSD-friendly apps with explicit geometry.

2. **`INFRA: launcher index empty / stale`** — first-toggle indexing
   only. A `launcher-rescan` ctrl command is the obvious fix
   (~5 LOC in `modules/launcher.py`). Until then, every test must
   rely on `fresh-vm-bootstrap.sh` having installed all needed apps
   before greetd-qdwin starts.

3. **`GAP: launcher accepts no real keyboard input`** — by design as
   of §6.6 S4 (launcher.py:17-23). All keystrokes must go through
   the ctrl-socket. A wl_keyboard grab on the launcher surface is
   tracked for §6.8 follow-up.
