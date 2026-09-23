# 14 — idle / DPMS: set_display_power (v26)

**What**: drive the v26 `set_display_power` request against the headless
compositor via the bystander's `displaypower` command — force all outputs
off, then on — and assert on the compositor's log line.

**Why**: v26 is the display-power half of the Power tab's idle policy. The
shell owns the idle *timing* (via the standard `ext-idle-notify-v1`, which
qdwin already implements and which the qdshell qml-plugin now binds as a
client) and the inactivity *action* (suspend/lock — a session decision); the
compositor only enacts display power. `set_display_power` is the load-bearing
new request: on the display-off idle the shell calls `set_display_power(0)`,
and on resume `set_display_power(1)`.

**Non-visual**: the headless backend has no DPMS (its outputs have a NULL
`set_dpms`), so `weston_output_power_off/on` are no-ops there — this scenario
asserts the request is accepted and logged (the count of outputs acted on),
not a real monitor power transition. Actual DPMS + the ext-idle-notify idle
trigger are exercised on the live DRM VM (`tests/gui/20-idle-dpms.md`).

## Setup

```bash
ID=14-idle-dpms
HT=tests/host
$HT/start.sh $ID
RUN() { $HT/ctrl.sh $ID "$@" >/dev/null; sleep 0.4; }
WLOG=/tmp/qdwin-host-tests/$ID/weston.log   # ht_log_weston $ID
```

The bystander binds qdwin_shell_v1 at v26.

## Steps

### S1 — force display off, then on

```bash
RUN displaypower 0
grep -q 'qdwin: set_display_power on=0' "$WLOG" && echo "S1a PASS" || echo "S1a FAIL"
RUN displaypower 1
grep -q 'qdwin: set_display_power on=1' "$WLOG" && echo "S1b PASS" || echo "S1b FAIL"
```

**Assert:** both `set_display_power on=0 (N output…)` and `on=1` lines appear;
no `libwayland: error` / NULL-opcode abort (bystander bound at v26).

### S2 — outputs forced back on at shell unbind

The compositor forces all outputs on when the shell binding drops, so a
crashed shell can't leave the screen dark. (`qdwin_shell_resource_destroy`
calls `qdwin_set_all_outputs_power(.., 1)` when `display_forced_off`.)

```bash
$HT/stop.sh $ID
```

## Result (validated 2026-05-29, headless)

S1a/S1b PASS: `qdwin: set_display_power on=0 (1 output)` and
`on=1 (1 output)`; no protocol error (bystander bound v26). DPMS itself is a
no-op on the headless backend (NULL set_dpms) — real power transitions are
VM-only.
