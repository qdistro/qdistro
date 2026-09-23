# 16 — nested-proxy identity hardening (origin_uid / input_sink / pixels)

**What**: drive `qdwin-nested-identity-probe` against the headless compositor and
check qdwin's nested-proxy identity hardening
(`qdwin_nested_manager_advertise_toplevel`, the input-sink peer-cred check,
and `qdwin_handle_bind_proxy_pixels` in `qdwin/qdwin.c`):

1. a client that asserts a FOREIGN `origin_uid` in `advertise_toplevel`
   does NOT get that uid honoured — qdwin overrides it back to the
   advertising client's kernel-resolved peer uid.
2. (VM-only, multi-uid) an `input_sink` socket owned by another uid is
   refused (the proxy stays display-only); a `bind_proxy_pixels` call from
   a different uid than the advertiser is rejected.

**Why**: `todo/security-hardening-carryforward.md` §"qdwin and nested
protocols" — client-supplied `origin_uid` and `input_sink` must not become
authority without peer/process verification, and `bind_proxy_pixels` must
be tied to the proxy owner, not a bare client assertion. `origin_uid`
feeds the per-uid colour chip and the broker `qdistro.nested.advertise`
authz decision; an unverified value lets a client mislabel an inner app's
owning uid.

**Non-visual**: this scenario asserts on probe exit codes + weston-log
branch evidence, not screenshots — fully scriptable, no vision step.

**Headless limitation (read before relying on this)**: the override fires
whenever the asserted `origin_uid != peer_uid`, so the override branch IS
exercisable single-uid: the probe simply asserts `getuid()+1`, which can
never equal its own peer uid. What a single-uid host CANNOT do is run the
advertising client (or an input-sink listener, or a pixel binder) under a
*second real uid*, so:

- the **origin_uid override** (S1) is fully tested headlessly.
- the **input_sink foreign-uid refusal** and **bind_proxy_pixels foreign
  owner refusal** need a second real uid (the multi-uid VM suite,
  `tests/gui/03-multi-uid-colours`) and are left as a VM follow-up
  (documented in S2/S3 below as PENDING-VM). The source-level invariant is
  still locked by `qdwin/test_nested_identity_policy.py` so a regression
  fails the host `meson test` suite without a VM.

## Setup

```bash
HT=tests/host
. "$HT/lib.sh"
PROBE="$QDWIN_INSTALL/bin/qdwin-nested-identity-probe"
```

## Steps

### S1 — asserted foreign origin_uid is OVERRIDDEN to the peer uid

Start qdwin with the role-host nested manager exposed (default) and the
probe at the session uid. The probe advertises with `origin_uid =
getuid()+1`.

```bash
ID=16-nested-origin
$HT/start.sh $ID --no-shell --no-terminal >/dev/null
RUNTIME=$(ht_runtime $ID); SOCK=$(ht_socket $ID); WLOG=$(ht_log_weston $ID)

if XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" "$PROBE"; then RC=0; else RC=$?; fi
# The override line names BOTH uids; require the peer-uid target to be this
# uid and the asserted one to be uid+1 so we can't false-pass on an
# unrelated log line.
MYUID=$(id -u)
if grep -qE "advertise origin_uid=$((MYUID+1)) disagrees with advertising client peer uid=$MYUID" "$WLOG"; then OVR=0; else OVR=1; fi
# And the synthesised proxy must carry the PEER uid, not the asserted one.
if grep -qE "nested-proxy: created handle=[0-9]+ uid=$MYUID " "$WLOG"; then PROXY=0; else PROXY=1; fi
$HT/stop.sh $ID
if [ "$RC" -eq 0 ] && [ "$OVR" -eq 0 ] && [ "$PROXY" -eq 0 ]; then echo "S1 PASS"; else
    echo "S1 FAIL (exit=$RC want 0; override-log hit=$OVR want 0; proxy-uid hit=$PROXY want 0)"; fi
```

**Assert (S1):** all three hold:
- `RC == 0` — advertise round-tripped and `configured` arrived (the proxy
  was created; the override is silent on the wire by design).
- weston log: `advertise origin_uid=<uid+1> disagrees with advertising
  client peer uid=<uid>; overriding...` — proves the override branch ran.
- weston log: `nested-proxy: created handle=N uid=<uid> ...` — proves the
  synthesised proxy carries the verified peer uid, NOT the asserted one.

### S2 — foreign-owned input_sink is refused (PENDING-VM)

A second real uid must `listen()` on a unix socket; the probe (uid A)
advertises that socket as its `input_sink`. qdwin connects, reads the
listener's `SO_PEERCRED` uid (= B), finds B != A, and refuses the sink
(proxy stays display-only). Headless single-uid cannot create a foreign
listener, so this is VM-only.

```bash
# PENDING-VM. Procedure for the multi-uid VM suite:
#   - uid B: socat/nc UNIX-LISTEN on /run/user/<B>/qdwin-nested-input-*.sock
#   - uid A: qdwin-nested-identity-probe --input-sink=<that path>
#   - assert weston log: "input-sink peer uid=<B> does not match origin
#     uid=<A> ... refusing sink, proxy is display-only"
```

**Assert (S2, VM):** weston log contains the `input-sink peer uid ... does
not match origin uid ... refusing sink` line and no input is ever written
to the foreign socket.

### S3 — bind_proxy_pixels from a foreign uid is refused (PENDING-VM)

uid A advertises a proxy; uid B (a separate qdwin_shell_v1 client at a
different real uid) calls `bind_proxy_pixels` on A's handle. qdwin compares
the caller's peer uid (B) to the proxy's verified `origin_uid` (A) and
posts `invalid_handle`, refusing the pixel hijack. Needs two real uids ⇒
VM-only.

```bash
# PENDING-VM. Procedure for the multi-uid VM suite:
#   - uid A: advertise a nested proxy (note its handle from the weston log)
#   - uid B: a qdwin_shell_v1 consumer calls bind_proxy_pixels(handle, surface)
#   - assert weston log: "bind_proxy_pixels refused handle=<h>: caller
#     uid=<B> != proxy origin uid=<A>" and the curtain/pixels are unchanged
```

**Assert (S3, VM):** weston log contains the `bind_proxy_pixels refused ...
caller uid ... != proxy origin uid` line; the proxy's pixel surface is not
replaced.

## Teardown

`stop.sh` runs inline per case. If S1 aborts before its `stop.sh`:
`$HT/stop.sh 16-nested-origin`.

## Pass criteria

S1 holds headlessly (RC==0 + override log + proxy-uid log). S2/S3 are
PENDING-VM and are tracked for the multi-uid VM suite; the source-level
enforcement they would exercise is locked by
`qdwin/test_nested_identity_policy.py` (run by `meson test`).
