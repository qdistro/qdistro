# 45 — `forever_basename` cross-binary: same basename hits, different basename misses

<!-- qci:visual: required -->

**What**: install `forever_basename` for `qsu /usr/bin/python3 -c …`.
Then issue three follow-up qsu calls and verify:
1. `qsu /usr/bin/python3 -c 'print("py2")'` → cache hit (same
   argv[0] basename; trailing args are not part of this scope).
2. `qsu /usr/local/bin/python3 -c 'print("py3")'` → cache hit (same
   basename `python3`, different argv[0] path).
3. `qsu /usr/bin/perl -e …` → re-prompt (basename `perl` ≠
   `python3`).

**Why**: `doc/sudo.md` Approval scope table calls
`forever_basename` (`match_kind=basename`) the right scope when
admin wants to "loosen the path; tighten the command identity" —
the daily admin reality is "I move binaries around between PATH
entries, but `python3` is `python3` is `python3` and I shouldn't
have to re-approve when an update shuffles paths." This is also
the most-likely-to-leak-by-collision scope (a basename like
`bash` matches more than admin probably meant). No GUI test
currently exercises the cross-path match;
`tests/integration/s57-qsu-argv-scopes.sh` covers it as a D-Bus
probe (note: that is the top-level `s57` driver, not
`permissions-gui/57`, which is unrelated).

## Setup

```bash
VM=${VMNAME:-qd-sudo}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec
VMGUI=${QDISTRO_REPO}/scripts/vm/vm-gui

$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml'
# Job records from an earlier attempt must not survive into this one: a
# leftover /tmp/45-*.rc would satisfy the first bg_wait instantly.
$VMEXEC "$VM" 'rm -f /tmp/45-*.log /tmp/45-*.rc /tmp/45-*.rc.part /tmp/45-*.pid /tmp/45-s2-created-at'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
$VMEXEC "$VM" 'systemctl restart qdistro-root-exec.socket'
sleep 1

B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"

# S4 needs a SECOND path whose basename is also `python3` — that is the
# whole point of the scope under test. A symlink under a different NAME
# (say `python3-alt`) would have basename `python3-alt` and must MISS, so
# it cannot stand in for this. Record whether we created the link, so
# Teardown removes only our own and never yanks a real install. (An
# interrupted run can leave the link without the marker; the next Setup
# then treats it as pre-existing and leaves it behind. Harmless in a
# disposable VM, which is the only place this scenario runs.)
$VMEXEC "$VM" 'rm -f /tmp/45-made-local-python3
if [ ! -e /usr/local/bin/python3 ]; then
    ln -s /usr/bin/python3 /usr/local/bin/python3 && : > /tmp/45-made-local-python3
fi
test -f /usr/local/bin/python3 && test -x /usr/local/bin/python3 \
    && basename "$(readlink -f /usr/local/bin/python3)"'

$VMEXEC "$VM" 'runuser -u admin -- /usr/local/bin/qdistro-start-admin-app'
sleep 3
```

**Assert**: `/usr/local/bin/python3` exists and is executable. If it
does not, S4 cannot be run — report the scenario as an ERROR rather
than substituting a different path.

## Steps

### S1 — first qsu /usr/bin/python3 pends

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 45-py1 work '/usr/local/bin/qsu /usr/bin/python3 -c "print(\"py1\")"'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# This sleep is NOT waiting for the job — the job must still be PENDING
# when the screenshot is taken. It gives the admin app time to render the
# new row.
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s1-pending.png
```

**Assert**: pending row visible; details contain `/usr/bin/python3`.

### S2 — admin picks `forever_basename` and approves

```bash
$VMGUI "$VM" screenshot /tmp/45-s2a-radios.png

# Select the `forever_basename` scope (7th radio, "Forever, this
# argv basename anywhere") via the admin app's direct scope
# shortcut. The scope keys map Ctrl+Shift+1..8 to
# once/1h/24h/forever/forever_exe/forever_argv/forever_basename/
# forever_prefix (admin_app/qdistro_admin_app.py:2040-2046), so
# `forever_basename` is Ctrl+Shift+7. Mouse clicks to Qt/XWayland
# are PLATFORM-BLOCKED on this template (AGENTS.md:166-176); the
# blessed input path is `virsh send-key` at the virtual evdev
# keyboard (AGENTS.md:146-164). Always windowactivate first so the
# approvals window holds X focus when the evdev event arrives.
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_LEFTSHIFT KEY_7
sleep 1
$VMGUI "$VM" screenshot /tmp/45-s2b-selected.png

# Approve the current request with the selected scope (Ctrl+Y →
# admin_app/qdistro_admin_app.py:2028).
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_Y

# The approval releases the qsu call. Wait for the job's OWN completion
# record — a pid file only says it started, and `wait` on that pid from
# this separate guest shell returns immediately (AGENTS.md, "A
# backgrounded job"). Reading the log before this returns is exactly how
# this step has reported empty output as a product failure.
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 45-py1 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 45-py1; echo "rc=$(bg_rc 45-py1)"'
# Gate S3–S5 on the cache row actually being what S2 decided. Save
# created_at so an S3 timeout dump can be compared with the later
# request's audit ts (both are unix-epoch seconds; equal values are
# unordered).
B64=$(base64 -w0 <<'EOF'
set -e
row=$(sqlite3 /var/lib/qdistro/approvals/approvals.sqlite \
  "SELECT match_kind, match_value, argv, scope, created_at
     FROM approvals WHERE action='qsu.exec:root'
     ORDER BY created_at DESC LIMIT 1;")
printf '%s\n' "$row"
kind=$(printf '%s\n' "$row" | cut -d'|' -f1)
argv=$(printf '%s\n' "$row" | cut -d'|' -f3)
scope=$(printf '%s\n' "$row" | cut -d'|' -f4)
created=$(printf '%s\n' "$row" | cut -d'|' -f5)
if [ "$kind" != basename ] || [ "$argv" != python3 ] \
        || [ "$scope" != forever_basename ] || [ -z "$created" ]; then
    echo "FAIL S2: expected basename||python3|forever_basename|<epoch>, got: $row" >&2
    exit 1
fi
printf '%s\n' "$created" > /tmp/45-s2-created-at
echo "S2_created_at=$created"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**:
- `/tmp/45-s2b-selected.png` shows `forever_basename` radio filled.
- the S1 job printed `py1` and `rc=0`.
- stdout contains `basename||python3|forever_basename|<epoch>` and
  `S2_created_at=<epoch>`. For basename rows the pattern lives in
  `argv` and `match_value` is empty.

**This assertion gates the rest of the scenario.** A missing or
wrong-kind row is an S2 failure; a later re-prompt then says nothing
about basename matching.

### S3 — same argv[0] basename, different `-c` payload → cache hit

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 45-py2 work '/usr/local/bin/qsu /usr/bin/python3 -c "print(\"py2\")"'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# if/else so a timeout still takes the screenshot before failing.
if $VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 45-py2 60'; then
    S3_WAIT=0
else
    S3_WAIT=$?
fi
$VMGUI "$VM" screenshot /tmp/45-s3-stillempty.png
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 45-py2; echo "rc=$(bg_rc 45-py2)"'
if [ "$S3_WAIT" -ne 0 ]; then
    $VMEXEC "$VM" 'echo S2_created_at=$(cat /tmp/45-s2-created-at 2>/dev/null || echo missing)' || true
    APPR_B64=$(base64 -w0 <<'SQL_EOF'
SELECT 'approval', created_at, match_kind, argv, scope FROM approvals
  WHERE action='qsu.exec:root' ORDER BY created_at;
SQL_EOF
    )
    AUD_B64=$(base64 -w0 <<'SQL_EOF'
SELECT 'audit', ts, action, decision, scope, source FROM audit
  WHERE action LIKE 'qsu.exec:%' ORDER BY ts;
SQL_EOF
    )
    $VMEXEC "$VM" "echo $APPR_B64 | base64 -d | sqlite3 /var/lib/qdistro/approvals/approvals.sqlite" || true
    $VMEXEC "$VM" "echo $AUD_B64 | base64 -d | sqlite3 /var/lib/qdistro/audit/audit.sqlite" || true
    $VMEXEC "$VM" 'journalctl -u qdistro-admin-broker.service --no-pager | tail -60' || true
    echo "FAIL S3: the call never completed (expected cache-hit path failed)" >&2
    exit 1
fi
```

**Assert**: `py2` with `rc=0`; `/tmp/45-s3-stillempty.png` shows an
empty pending list.

A `bg_wait` TIMEOUT is a failure of the expected cache-hit path, not
proof of a cache miss. The dump prints S2's saved `created_at` next to
the current approval and audit rows so those timestamps can be
compared; equal epoch seconds are unordered. A 60s-old pending row may
already have expired, so a blank screenshot is not evidence either way.

### S4 — different argv[0] path, SAME basename → cache hit

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 45-py3 work '/usr/local/bin/qsu /usr/local/bin/python3 -c "print(\"py3\")"'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
if $VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 45-py3 60'; then
    S4_WAIT=0
else
    S4_WAIT=$?
fi
$VMGUI "$VM" screenshot /tmp/45-s4-stillempty.png
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 45-py3; echo "rc=$(bg_rc 45-py3)"'
if [ "$S4_WAIT" -ne 0 ]; then
    echo "FAIL S4: the call never completed (expected cache-hit path failed)" >&2
    exit 1
fi
```

**Assert**:
- `py3` with `rc=0` — the cache matched on basename `python3` even
  though argv[0] is `/usr/local/bin/python3`, not the
  `/usr/bin/python3` that was approved.
- `/tmp/45-s4-stillempty.png`: pending list empty (cache hit).

### S5 — different basename (`perl`) → re-prompt

```bash
B64=$(base64 -w0 <<'EOF'
source /tmp/qci-gui-waiters.sh
bg_start 45-perl work '/usr/local/bin/qsu /usr/bin/perl -e "print qq(perl1\\n)"'
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
# Deliberately do NOT bg_wait here: the expected outcome is that this
# call BLOCKS on an admin decision. The screenshot is the assertion.
sleep 2
$VMGUI "$VM" screenshot /tmp/45-s5-pending.png
```

**Assert** (`/tmp/45-s5-pending.png`): one pending row, details
show `argv=/usr/bin/perl -e ...`. Basename `perl` ≠ `python3` →
cache row did NOT match.

### S6 — deny perl to clean up

```bash
B64=$(base64 -w0 <<'EOF'
runuser -u admin -- env DISPLAY=:0 xdotool search --sync \
  --name "admin approvals" windowactivate --sync
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_wait 45-perl 60'
$VMEXEC "$VM" 'source /tmp/qci-gui-waiters.sh; bg_log 45-perl; echo "rc=$(bg_rc 45-perl)"'
```

**Assert**: log contains `request denied`, and `rc` is nonzero.

## Teardown

```bash
$VMEXEC "$VM" 'pkill -u admin -f qdistro_admin_app 2>/dev/null; true'
$VMEXEC "$VM" 'pkill -u work -f qsu 2>/dev/null; true'
$VMEXEC "$VM" 'rm -f /tmp/45-*.log /tmp/45-*.rc /tmp/45-*.rc.part /tmp/45-*.pid /tmp/45-s2-created-at'
# Remove /usr/local/bin/python3 only if SETUP created it — the marker
# file, not a readlink guess, is what distinguishes our link from a real
# local install that happened to point at /usr/bin/python3.
$VMEXEC "$VM" 'if [ -e /tmp/45-made-local-python3 ]; then rm -f /usr/local/bin/python3; fi
rm -f /tmp/45-made-local-python3'
B64=$(base64 -w0 <<'EOF'
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE 'qsu.exec:%';"
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

## Notes for the runner

- The cache layer stores match_value as `basename(argv[0])` at
  decide time, and at lookup time computes `basename(req.argv[0])`
  the same way. A bug where one side uses `os.path.basename` and
  the other uses `argv[0].rsplit("/")[-1]` would only show up if
  argv[0] has unusual path components (e.g. `./python3`). Not
  worth a separate scenario; mention in notes if you find one
  during teardown.
- This scenario is the GUI counterpart of
  `tests/integration/s57-qsu-argv-scopes.sh`'s `forever_basename`
  phase 2 ("different argv[0] same basename → hit"). The point of
  repeating it here is to pin the admin app surface: that the radio
  labelled `Forever, this argv basename anywhere` actually maps to
  the broker's `forever_basename` scope key (not, say, the legacy
  `forever_exe` due to a label/value mismatch in the
  `_scope_buttons` tuple). When S3/S4 re-prompt but that s57 phase
  passes, the defect is in this surface, not in the cache.
