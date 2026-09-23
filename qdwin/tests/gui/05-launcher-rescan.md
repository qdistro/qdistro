# 05 — launcher rescans after app install (B5)

<!-- qci:visual: required -->

**Acceptance criterion:** when an application is installed via zypper
after the qdwin session has started, the new app appears in the
launcher on the next Ctrl+Space toggle (no greetd-qdwin restart
required). Validates the mtime-based auto-rescan in
`launcher_toggle` and the `launcher-rescan` ctrl command.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"

pgrep -f "http.server 8765" >/dev/null || (
    cd ${QDWIN_REPO} && \
    python3 -m http.server 8765 --bind 127.0.0.1 >/tmp/qdistro-http.log 2>&1 &
)
sleep 1
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }
```

## Steps

### Step 1 — open launcher to seed the index

```bash
qdwin_ctrl "launcher-toggle" >/dev/null
qdwin_ctrl "launcher"
```

**Assert (1.1):** `indexed=N` for some N≥4 (Foot family + Remote
Viewer at minimum on the standard --from-baked clone). Note this
value as `N0` for step 3.

### Step 2 — install a new app while session is running

```bash
qdwin_ctrl "launcher-toggle" >/dev/null  # close

# Pick an app with a sensible .desktop entry, that's small + fast.
cat > ${QDWIN_REPO}/extra/install-extra.sh <<'EOF'
zypper -n install qpdfview 2>&1 | tail -3
ls /usr/share/applications/qpdfview.desktop
EOF
# CAPTURE THROUGH A FILE, NOT `... 2>&1 | tail`. A host pipeline hands vm-exec's
# fd 2 to the pipe and the shell then waits for `tail` to see EOF on its stdin,
# which happens only when the LAST writer closes it. vm-exec bounds its own
# children's fd 1 internally, but fd 2 goes straight through to every virsh/jq
# descendant, and one that outlives vm-exec holds this step open after the guest
# install is long dead. Reading a regular file has no such dependency.
_IEA_LOG=$(mktemp "${TMPDIR:-/tmp}/qdwin05-install.XXXXXXXX")
"$QDWIN_VM_EXEC" "$VMNAME" 'wget -qO /tmp/iea.sh http://10.0.2.2:8765/extra/install-extra.sh && bash /tmp/iea.sh' \
    > "$_IEA_LOG" 2>&1 || :
# `tail -5` is a LINE bound, not a byte bound: one arbitrarily long line
# replays in full (astra, A-astra finding 5). Bound the bytes first, then
# take the last lines of that bounded tail.
tail -c "${QDWIN_05_LOG_ECHO_BYTES:-65536}" "$_IEA_LOG" | tail -5; rm -f "$_IEA_LOG"
```

**Assert (2.1):** `qpdfview.desktop` lands in
`/usr/share/applications/`. (If qpdfview is already present from a
prior run, the test still proceeds — the rescan should still pick
it up either way.)

### Step 3 — toggle launcher; auto-rescan should fire

```bash
qdwin_ctrl "launcher-toggle" >/dev/null
sleep 0.3
qdwin_ctrl "launcher"
```

**Assert (3.1):** `indexed >= N0`. If the app was newly installed by
step 2, `indexed = N0 + 1`. If qpdfview was already present from a
prior bootstrap (idempotent on re-runs against a long-lived VM),
`indexed = N0` is also valid — the test's load-bearing check is
that the new app is present in the filtered list (assert 4.1+4.2),
not the absolute count delta.

### Step 4 — filter for the new app

```bash
qdwin_ctrl "launcher-type qpdf" >/dev/null
qdwin_screenshot /tmp/05-step4-qpdf-visible.png
qdwin_ctrl "launcher"
```

**Assert (4.1):** `matches>=1`.
**Assert (4.2):** screenshot shows a row labelled `qpdfview` in the
overlay.

### Step 5 — explicit launcher-rescan ctrl command

```bash
qdwin_ctrl "launcher-toggle" >/dev/null  # close
sleep 0.3
qdwin_ctrl "launcher-rescan"
qdwin_ctrl "launcher"
```

**Assert (5.1):** `launcher-rescan` returns `ok launcher-rescan
indexed=N` where N matches the post-install count.

## Cleanup

(none required; qpdfview installed package is harmless to leave in
place; subsequent test runs are idempotent)

## Pass criteria

All asserts 1.1 → 5.1 pass. Confirms:
- `Launcher.indexed_mtime` field tracks the last-scanned mtime.
- `launcher_toggle` calls `_app_dirs_mtime()` and rescans if mtime
  advanced.
- `launcher-rescan` ctrl-socket command rebuilds the index on demand.

## Known-broken-if

- Step 3 indexed count doesn't increase → mtime auto-rescan is
  broken. Verify launcher_toggle in
  `qdshell/Modules/Launcher (QML)` calls `_app_dirs_mtime()`
  and compares to `ln.indexed_mtime`.
- Step 4 matches=0 → install path didn't actually drop the .desktop
  file. Run `ls /usr/share/applications/` in the VM to confirm.
