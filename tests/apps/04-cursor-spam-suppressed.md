# 04 — install_default_cursor warning logs at most once per session

<!-- qci:visual: none -->

**Acceptance criterion:** the `qdwin: install_default_cursor: no
surface yet` log line fires at most once per qdwin session, not on
every pointer focus change. Regression-tests qdwin bug #4 (one-shot
flag added in `qdwin_install_default_cursor_on_pointer`).

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"
qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
qdwin_apps_kill_all
```

## Steps

### Step 1 — capture the compositor journal boundary

```bash
JOURNAL_CURSOR=$(qdwin_apps_journal_cursor)
test -n "$JOURNAL_CURSOR" || {
    echo "ERROR: could not capture qdwin-compositor.service journal cursor" >&2
    exit 1
}
echo "journal cursor: $JOURNAL_CURSOR"
```

### Step 2 — exercise pointer focus changes

Launch and close several apps in sequence to provoke focus change
events (each toplevel arrival/destruction can trigger a default-cursor
install attempt).

```bash
for app in foot foot foot; do
    qdwin_apps_launch "$app" "$app"
    sleep 2
    qdwin_apps_kill_all
    sleep 1
done
qdwin_apps_launch xterm "xterm"
sleep 2
qdwin_apps_kill_all
sleep 1
```

### Step 3 — inspect only lines caused by the exercise

```bash
NEW_WARNINGS=$(qdwin_apps_log_since_cursor "$JOURNAL_CURSOR" \
    'install_default_cursor: no surface yet' || true)
if [ -n "$NEW_WARNINGS" ]; then
    COUNT_AFTER=$(printf '%s\n' "$NEW_WARNINGS" | grep -c .)
else
    COUNT_AFTER=0
fi
printf '%s\n' "$NEW_WARNINGS"
echo "new warnings: $COUNT_AFTER"
[ "$COUNT_AFTER" -le 1 ] || {
    echo "FAIL: expected at most 1 new cursor warning, observed $COUNT_AFTER" >&2
    exit 1
}
if [ "$COUNT_AFTER" -eq 1 ]; then
    printf '%s\n' "$NEW_WARNINGS" \
        | grep -Fq '(helper not started?) — further occurrences suppressed' || {
            echo "FAIL: one cursor warning was emitted without the suppression hint" >&2
            exit 1
        }
fi
```

**Assert (3.1):** `COUNT_AFTER <= 1`. The fix lets the
warning fire once per session and then suppresses; without the fix
each app cycle adds 5–20 lines.
**Assert (3.2):** if the warning fired at all, the message must
include the suppression hint:
`(helper not started?) — further occurrences suppressed`. Grep for
that exact string in `NEW_WARNINGS`.

## Cleanup

```bash
qdwin_apps_kill_all
qdwin_apps_restore_shell
```

## Pass criteria

- New journal lines after the captured cursor ≤ 1.
- The single line (if any) carries the "suppressed" suffix.

## Known failure modes

- **Pre-fix regression (bug #4)** — `COUNT_AFTER - COUNT_BEFORE` is
  in the dozens or hundreds. The one-shot flag at
  `qdwin_install_default_cursor_on_pointer` regressed.
- **qdistro-cursor-sprites helper running** — the warning never fires
  at all (helper installed the default sprite). That's also a pass,
  but not what we're testing. Confirm via
  `pgrep -af qdistro-cursor-sprites` returning nothing.
