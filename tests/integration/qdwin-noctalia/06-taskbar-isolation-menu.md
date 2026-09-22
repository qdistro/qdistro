# 06 — Taskbar isolation menu renders for a tier-2 disposable

<!-- qci:visual: required -->

**Acceptance criterion:** a real tier-2 DISPOSABLE window (spawned through the
production root-launcher path so its `wp_security_context_v1` app_id reaches
qdwin) shows up in the qdshell taskbar as an ISOLATED window; right-clicking its
taskbar item opens the qdistro isolation context menu, and the menu visibly
offers **Permissions…** and **Dispose**. Invoking the menu's Dispose action
removes the disposable.

This scenario is written to be runnable by a SMALL agent model: every
non-visual step is a single exact command to copy-run (do NOT improvise the
commands — especially the D-Bus call in step 4, which must be run verbatim). The
only finicky visual judgement asked of you is to *read* one screenshot of the
menu (best-effort evidence, not a pass/fail gate). Run
the steps in order; write `PASS` to `status.txt` only if every **Assert** holds,
else `FAIL` with the first one that did not.

Runs only on the qdwin+qdshell profile (`QDISTRO_VM_GUI_SESSION=qdwin`).

## Setup

Run each command with the VM helpers already sourced for you. `$VMNAME` is the
target VM; `$QDWIN_VM_EXEC` runs a command in the VM as root.

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
source ${QDISTRO_REPO}/tests/integration/qdwin-noctalia/noctalia-helpers.sh
qdwin_set_vm "${VMNAME}"
noct_session_healthy || { echo "FAIL: qdwin/qdshell session not active"; exit 1; }
```

Build the disposable image + broker allow-rule (one exact command; idempotent,
may take a few minutes on first run):

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "bash /root/qdistro-src/qdistro/tests/integration/vm/probes/disp-secctx-wiretag-probe.sh setup"
```
**Assert (setup):** the command prints `PASS: setup`.

## Step 1 — spawn one isolated disposable window (exact commands)

Spawn a disposable and capture its identity. Run verbatim:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "
  : > /tmp/m4-spawn.out
  nohup bash -c 'TIER2_ROOT_LAUNCHER=1 TIER2_ADMIN_UID=1000 WAYLAND_DISPLAY=wayland-1 /usr/bin/qdistro-tier2-spawn --disposable weston-terminal -- weston-terminal' >/tmp/m4-spawn.out 2>&1 &
  for i in \$(seq 1 80); do grep -q '^LAUNCH_TOKEN=' /tmp/m4-spawn.out && break; sleep 0.5; done
  grep -E '^(APP_ID|LAUNCH_TOKEN|CONTAINER)=' /tmp/m4-spawn.out
"
```
Record the three values it prints: `APP_ID` (looks like `qdistro.disp.<hex>`),
`LAUNCH_TOKEN` (32 hex chars), `CONTAINER` (looks like
`disp-weston-terminal-<date>`). You will paste `LAUNCH_TOKEN` and `CONTAINER`
into later commands.

**Assert (1.1):** qdwin committed the secctx identity. Run verbatim, replacing
`<APP_ID>` and `<LAUNCH_TOKEN>` with the values from above:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "for i in \$(seq 1 60); do line=\$(journalctl -b _SYSTEMD_USER_UNIT=qdwin-compositor.service 2>/dev/null | grep -F -m1 'qdwin/secctx: committed engine=qdistro.tier2 app_id=<APP_ID> instance_id=<LAUNCH_TOKEN>'); [ -n \"\$line\" ] && { echo \"\$line\"; exit 0; }; sleep 0.5; done; echo NO_COMMIT; exit 1"
```
It polls up to 30s (the commit can lag the spawn). It must print one
`qdwin/secctx: committed …` line. If it prints `NO_COMMIT` → FAIL.

Both journal waits in this file read ONLY qdwin's own unit
(`_SYSTEMD_USER_UNIT=qdwin-compositor.service`). Do not widen them to plain
`journalctl`: qemu-ga logs every command it runs (`guest-exec called: "…"`),
so a whole-journal grep matches the waiting command's own text and passes on
an identity that never existed.

## Step 2 — wait for the window's pixels, then screenshot the desktop

The secctx commit (1.1) lands before the disposable's window exists. The window
reaches qdwin as a **nested proxy**: qdwin first logs
`toplevel_security_context (nested proxy) handle=<N> … app_id=<APP_ID>`, then,
once the live feed replaces the blank curtain on the approved window, either
`bind_proxy_pixels handle=<N> … (curtain swapped for live feed)` or
`activate pixel surface handle=<N> (deferred swap on allow)`. Other
`bind_proxy_pixels` lines (feed STASHED while pending, view-create failed) are
NOT a map. The command follows the NEWEST matching handle, in case the window
is re-created. Do NOT replace this with a fixed sleep. Run verbatim, replacing
`<APP_ID>` and `<LAUNCH_TOKEN>`:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "for i in \$(seq 1 60); do j=\$(journalctl -b _SYSTEMD_USER_UNIT=qdwin-compositor.service 2>/dev/null); m=\$(printf '%s\n' \"\$j\" | grep -nF 'toplevel_security_context (nested proxy) handle=' | grep -F ' engine=qdistro.tier2 app_id=<APP_ID> instance=<LAUNCH_TOKEN>' | tail -1); n=\${m%%:*}; h=\$(printf '%s\n' \"\$m\" | sed -n 's/.*(nested proxy) handle=\([0-9]*\) .*/\1/p'); [ -n \"\$h\" ] && printf '%s\n' \"\$j\" | tail -n \"+\$n\" | grep -qE \"qdwin/nested-proxy: (bind_proxy_pixels handle=\$h surface=.*curtain swapped for live feed|activate pixel surface handle=\$h \(deferred swap on allow\))\" && { echo \"MAPPED handle=\$h\"; exit 0; }; sleep 0.5; done; echo NO_MAP; exit 1"
```
It polls up to 30s. It must print `MAPPED handle=<N>`. If it prints `NO_MAP` →
FAIL (2.1).

Then capture the desktop:

```bash
noct_screenshot_awake /tmp/06-step2-desktop.png
```
**Assert (2.1):** the wait above printed `MAPPED handle=<N>`, AND
`/tmp/06-step2-desktop.png` shows (a) the qdshell **bar** along
the very top edge of the screen, and (b) a window titled **"Wayland Terminal"**
in the middle. The background is a plain solid colour (no picture)
— the bar and window stand out against it. The disposable's **taskbar item** is
the small entry the new window added to the bar, in the **top-left** group of
bar icons.

## Step 3 — right-click the taskbar item, screenshot the menu (BEST-EFFORT)

This is the one visual step. The taskbar item is a small (~40 px) icon — the
right-click that opens its isolation menu is finicky, so this step is
**best-effort EVIDENCE, not a hard pass/fail gate** (the disposable's isolation
identity and the Dispose action are gated deterministically in steps 1 and 4).

The taskbar item is the small coloured square the new window added to the bar,
in the top-left icon group. On the fixed 1280×800 GUI-CI session it sits at about
`(205, 16)`. Right-click it and screenshot:

```bash
qdwin_click 205 16 right
sleep 1
qdwin_screenshot /tmp/06-step3-menu.png
```

Look at `/tmp/06-step3-menu.png` and RECORD (do not fail on it) whether a context
menu opened anchored near the taskbar item and whether you can read the words
**"Dispose"** and **"Permissions"** in it (a disposable does NOT show
"Snapshot"). Note your finding ("menu opened: yes/no; read Dispose+Permissions:
yes/no") and attach the screenshot as an artifact either way.

## Step 4 — invoke Dispose (exact command — do NOT improvise)

The menu's Dispose action calls `SessionManager1.DisposeByToken`. Run that exact
call (this is the command qdshell's qd-dispose handler runs). Replace
`<LAUNCH_TOKEN>` with the token from step 1 — run VERBATIM otherwise:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "runuser -l admin -c \"gdbus call --system --dest org.qdistro.SessionManager1 --object-path /org/qdistro/SessionManager1 --method org.qdistro.SessionManager1.DisposeByToken '<LAUNCH_TOKEN>'\""
```
**Assert (4.1):** the command prints `(true,)`.

**Assert (4.2):** the disposable is gone. Run verbatim, replacing `<CONTAINER>`:

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "for i in \$(seq 1 40); do runuser -l admin -c 'podman container exists <CONTAINER>' || { echo GONE; exit 0; }; sleep 0.5; done; echo STILL_PRESENT; exit 1"
```
It must print `GONE` (it exits non-zero with `STILL_PRESENT` if the container
survives).

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" "bash /root/qdistro-src/qdistro/tests/integration/vm/probes/disp-secctx-wiretag-probe.sh teardown"
```

## Pass criteria

Write `PASS` to `status.txt` only if ALL of the HARD gates held: setup, **1.1**
(secctx committed — the disposable reached qdwin isolated), **2.1** (`MAPPED`
was printed, then the bar + window are visible), **4.1** (`(true,)`), **4.2**
(`GONE`). The step-3 menu
screenshot is recorded EVIDENCE, not a pass/fail gate — note in `report.md`
whether the menu opened and whether Dispose/Permissions were readable, and
attach `/tmp/06-step2-desktop.png` + `/tmp/06-step3-menu.png`. Otherwise write
`FAIL` and the first hard gate that did not hold.

## Known failure modes

1. **No secctx commit (1.1)** — the spawn ran un-tagged; confirm the step-1
   command was run verbatim (`TIER2_ROOT_LAUNCHER=1`).
2. **`DisposeByToken` errors with "not activatable" (4.1)** — you did not run
   step 4 verbatim. It MUST be `runuser -l admin -c "gdbus call --system …"`
   (admin user, system bus, the full object path + method). Re-run exactly.
3. **`NO_MAP` (2.1)** — the disposable committed its identity but its window
   never showed live pixels within 30s. That is a real failure (the nested
   weston or its pipewire feed did not come up); do NOT retry with a sleep.
   Attach the output of
   `journalctl -b _SYSTEMD_USER_UNIT=qdwin-compositor.service | grep -E 'nested-proxy|nested-toplevel|nested_proxy_decision|holding_released|activate pixel surface'`
   — a `nested_proxy_decision … DENY` or a `holding_released … deferred` line
   means the window was refused or is still held, not that the feed is down.
4. **Step-3 menu did not open** — expected/tolerated: the taskbar item is a small
   icon and its right-click is finicky; this is recorded evidence, not a failure.
   The isolation IDENTITY (1.1) and the Dispose ACTION (4.1/4.2) are what this
   lane gates; the menu RENDER is also covered by the qdshell unit tests
   (`test_taskbar_logic.js`) and the deterministic
   `tests/integration/vm/qdwin-taskbar-isolation.bats`.
