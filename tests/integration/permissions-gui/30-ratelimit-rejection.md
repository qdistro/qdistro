# 30 — Rate-limit raises `.RateLimited` on a tight-loop caller

**What**: from `work`, call `CheckPermission` for `test.action` in a
tight Python loop. After `LIMIT` successful calls (the broker pins
`LIMIT=50, WINDOW_S=1.0` at construction in
`broker/qdistro_admin_broker.py` line ~357), the next call within
the same 1-second window must raise the D-Bus error
`com.qdistro.AdminBroker1.RateLimited`. Verify the error name, that
the threshold is exactly 50 calls, and that a fresh request after
the window has elapsed succeeds again.

**Why**: `permissions.md` mentions rate-limiting only in passing,
but the broker enforces it (see `qdistro_admin_ratelimit.py`).
The contract under load is that one misbehaving uid can't pin the
broker's rule/cache engine in a tight loop. Without enforcement,
a compromised user process could DoS the admin's permission
infrastructure invisibly.

This is a headless scenario.

## Setup

```bash
VM=${VMNAME:-qdistro-dev-260421-1336}
VMEXEC=${QDISTRO_REPO}/scripts/vm/vm-exec

# Restart drops the in-memory rate-limit bucket so we start at 0.
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
sleep 1
```

## Steps

### S1 — tight loop: 50 calls succeed, 51st raises `.RateLimited`

```bash
# One python process holds the system bus connection so the loop
# is bounded by D-Bus roundtrip latency only (single-digit
# milliseconds), well under the 1-second window. A bash for-loop
# spawning dbus-send 50× would take seconds and the boundary
# would be non-deterministic.
B64=$(base64 -w0 <<'EOF'
sudo -u work python3 - <<'PY' >/tmp/30-s1.out 2>&1
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object("com.qdistro.AdminBroker1",
                       "/com/qdistro/AdminBroker1")
ok = 0
err_name = None
err_msg = None
for i in range(60):  # try 10 past the limit
    try:
        result = proxy.CheckPermission(
            "test.action", {"i": str(i)},
            dbus_interface="com.qdistro.AdminBroker1")
        if str(result) == "unknown":
            ok += 1
        else:
            print(f"unexpected verdict at i={i}: {result!r}")
            break
    except dbus.DBusException as e:
        err_name = e.get_dbus_name()
        err_msg = e.get_dbus_message()
        print(f"raised at i={i}: name={err_name!r}")
        print(f"  msg={err_msg!r}")
        break
print(f"ok_before_raise={ok}")
print(f"err_name={err_name}")
PY
cat /tmp/30-s1.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert** (textual analysis of `/tmp/30-s1.out`):
- `ok_before_raise=50` — exactly 50 calls returned `"unknown"`
  before the limiter fired.
- `err_name=com.qdistro.AdminBroker1.RateLimited` — the 51st call
  raised the typed error name.
- The accompanying `err_msg` mentions `uid=2000`, `'test.action'`,
  and the configured limit/window pair (`>50/1.0s` substring).

### S2 — same uid, *different* action is NOT rate-limited

```bash
# The limiter is keyed by (uid, action). A second action under
# the same uid has its own bucket.
B64=$(base64 -w0 <<'EOF'
sudo -u work python3 - <<'PY' >/tmp/30-s2.out 2>&1
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object("com.qdistro.AdminBroker1",
                       "/com/qdistro/AdminBroker1")
result = proxy.CheckPermission(
    "test.other-action", {"i": "0"},
    dbus_interface="com.qdistro.AdminBroker1")
print(f"other-action verdict={result!r}")
PY
cat /tmp/30-s2.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: `/tmp/30-s2.out` reads `other-action verdict='unknown'`.
The original action's saturated bucket did not bleed into a
different action.

### S3 — after the window elapses, the original action succeeds

```bash
# Window is pinned at 1.0s; sleep 2s for margin.
sleep 2
B64=$(base64 -w0 <<'EOF'
sudo -u work python3 - <<'PY' >/tmp/30-s3.out 2>&1
import dbus
bus = dbus.SystemBus()
proxy = bus.get_object("com.qdistro.AdminBroker1",
                       "/com/qdistro/AdminBroker1")
result = proxy.CheckPermission(
    "test.action", {"i": "postwindow"},
    dbus_interface="com.qdistro.AdminBroker1")
print(f"postwindow verdict={result!r}")
PY
cat /tmp/30-s3.out
EOF
)
$VMEXEC "$VM" "echo $B64 | base64 -d | bash"
```

**Assert**: `/tmp/30-s3.out` reads `postwindow verdict='unknown'`,
NOT a raised `.RateLimited`. The deque self-trims rows older than
1.0s on each `check()`, so after the 2-second wait the bucket is
empty.

## Teardown

```bash
$VMEXEC "$VM" 'rm -f /tmp/30-*.out'
$VMEXEC "$VM" 'systemctl restart qdistro-admin-broker.service'
```

## Notes for the runner

- The broker's `RateLimiter(limit=50, window_s=1.0)` is constructed
  in `qdistro_admin_broker.py:357` — these are not exposed over
  D-Bus or settable from config today. A future configurability
  patch would change S1's hard-coded `50`; track via the rate-limit
  message text the broker raises (it includes both numbers).
- The python tight loop is the only deterministic way to hit the
  1-second window. Avoid bash `for` + `dbus-send` here; the
  process-spawn overhead would make S1's "ok_before_raise" depend
  on host load.
- This scenario also exercises `CheckPermission`'s rate-limit
  branch, which is the same `self.ratelimit.check()` call used by
  `RequestPermission`, `CheckClipboardTransfer`,
  `CheckClipboardReceive`, and `CheckHandoffActivation`. One pass
  here pins the contract for all five entry points.
