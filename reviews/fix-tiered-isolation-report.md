# Root-cause: tiered-isolation.bats (full-20260805T162915Z-1550111)

**Run:** `full-20260805T162915Z-1550111`  
**File:** `tests/integration/vm/tiered-isolation.bats`  
**Exit:** 35  
**VM:** `qci-bats-tiered-isolation-260805-185523-1722968-7721`  
**Main tip at failure:** `2c358cf`  
**Worktree:** `/home/play2/qdistro/.worktrees/qdistro-tiered-isolation`  
**Branch:** `fix/tiered-isolation-clipboard-gate`

## Executive summary

| Class | Tests | Verdict |
| --- | --- | --- |
| **Mislabelled as fail-open (actually transport flake)** | 26 (s110) | Not a product ClipboardGate fail-open. Empty dbus verdicts under host suspend thrash; same-day earlier full runs passed s110 14/0. Solo retest on preserved VM: **14/0 PASS**. |
| **Real product gap (admin-control peer allowlist)** | 37 (s57) | `RevokeAllForUid` missing from `_ADMIN_CONTROL_STDIN_METHODS`, so `python3 -c` admin probes (s57 cleanup) were AccessDenied. Fix + longer dbus timeout. Solo retest after patch: **13/0 PASS**. |
| **Nested-virt / host-load flake** | 21, 23, 24 | Tier-5 QGA/boot under concurrent full-QCI + 32s host suspend jumps. Not gutted; no timeout inflate without better nested evidence. |
| **Cascade after thrash** | 28–32 | Outer bats VM QGA dead in `setup()` after earlier thrash. |

**Product security:** ClipboardGate.qml and broker unit tests are fail-closed. Empty probe string ≠ `allow`. Real fail-open would be `verdict=allow` on cross-silo/unverified same-silo — **not observed**.

---

## Test 26 (s110) — primary investigation

### Failure surface (CI log)
```
FAIL: cross-silo subscription NOT denied (broker returned '')
FAIL: ClipboardGate did NOT default-deny the cross-silo transfer
FAIL: no audit/journal evidence ... src_app=...
FAIL: rich-MIME gate wrong: a fail-open would let image/png cross the silo
FAIL: FAIL-OPEN: unverified same-silo clipboard receive returned ''
FAIL: input-forwarding authorization was not default-denied
[s110] 7 passes, 6 failures
```

### Why this is **not** a product fail-open
1. **All six failures returned empty string `''`, never `allow`.** Empty comes from parsing dbus-send output when the reply is `NoReply` / `ServiceUnknown` / `AccessDenied` / timeout — there is no `string "…"` payload.
2. **Same code, same day, earlier full runs:**
   - `full-20260805T075805Z-83338` → **ok 26**
   - `full-20260805T131123Z-810555` → **ok 26**
3. **Test 25 (s46 tier4 clipboard gate) PASSed in the same failing run** minutes before s110 — same `CheckClipboardTransfer` default-deny path.
4. **Unit tests** (`test_broker_clipboard_{transfer,receive}.py`, `test_broker_handoff_activation.py`) assert cross-silo default-deny and Option-B unverified same-silo deny.
5. **qdshell ClipboardGate.qml** documents: *“Broker failures are fail-closed: absent broker, timeout, malformed reply, and unknown verdict all deny and clear.”*
6. **Solo retest** of updated s110 on the preserved VM (host quieter): **`[s110] 14 passes, 0 failures`**.

### Contributing conditions in the failed run
- Sibling tier-5 probe (test 24) logged **host uptime jumps of 32s** (suspend/resume under memory pressure).
- Full QCI concurrent with many qemu guests (load ~10).
- `setup()` stops the broker every test; s110 only waited on `systemctl is-active` + `sleep 1`, not bus-name **Ping** (Type=simple / dbus activation race under load).
- bats wrapper discarded s110 stderr (`2>/dev/null`), hiding raw dbus errors.
- Probe treated empty as FAIL-OPEN wording → false security alarm.

### Fixes applied (s110 + bats)
- `ensure_broker_ready()`: start + **Peer.Ping** wait; mid-wait restart if wedged.
- `broker_gate_verdict()`: 30s reply-timeout, retries on NoReply/empty, logs raw reply.
- Journal fallback for transfer deny (mirror s46); **only** concrete `allow` labelled FAIL-OPEN.
- bats: start broker before driver; **keep stderr**.

---

## Test 37 (s57 qsu-argv-scopes)

### Failure surface
```
DecideRequest(...) timed out after 10 seconds
RevokeAllForUid(65534) timed out after 10 seconds
```
Broker was “up”; ok 38 real-flow still passed.

### Root causes
1. **Product:** `_ADMIN_CONTROL_STDIN_METHODS` listed `DecideRequest` / `GetPending` / `SaveRule` etc. but **not `RevokeAllForUid`**. s57 drives admin control via `runuser -u admin -- python3 -c …`. Without the allowlist entry, `RevokeAllForUid` is AccessDenied → cache not drained → later install requests can skip GetPending (observed on dirty preserved VM).
2. **Load:** 10s subprocess timeout loses to **30s+ host suspend** thrash → TimeoutExpired even when broker is healthy.

### Fixes
- Add `"RevokeAllForUid"` to `_ADMIN_CONTROL_STDIN_METHODS` in `broker/qdistro_admin_broker.py`.
- Unit test: `test_admin_python_c_can_revoke_all_for_uid`.
- s57: `_DBUS_SUBPROC_TIMEOUT_S = 60`.

### Retest
- After patching broker on preserved VM + updated s57: **`[s57] 13 passes, 0 failures`**.

---

## Tier-5 (21 / 23 / 24) and cascade (28–32)

| Test | Symptom | Classification |
| --- | --- | --- |
| 21 s45 | Domain running; QGA never within 180s; publisher never | Nested-on-nested under host thrash |
| 23 s49 | Domain never running+lifecycle within 120s | Nested boot starved |
| 24 s47 | Long vm-exec wait; host suspend 32s; QGA dead | Host overload |
| 28–32 | `setup`: Guest agent not responding | Outer VM QGA cascade after thrash |

**Same-day midday full run** (`131123Z`) passed 21–26 and 37 under lighter contention.

**No coverage gutting.** Nested QGA deadlines already 180s on s45; further inflate without clean solo repro risks masking real guest-image bugs. Residual risk: under concurrent full-QCI + nested virt, tier-5 and cascade flakes can recur.

---

## Changes (worktree)

| File | Change |
| --- | --- |
| `broker/qdistro_admin_broker.py` | `RevokeAllForUid` ∈ `_ADMIN_CONTROL_STDIN_METHODS` |
| `tests/unit/test_broker_control_plane_identity.py` | Peer allowlist unit test |
| `tests/integration/vm/s110-tier4-waypipe-display.sh` | Broker Ping + retrying gate probes + honest FAIL-OPEN wording |
| `tests/integration/vm/s57-qsu-argv-scopes.sh` | 60s dbus subprocess timeout |
| `tests/integration/vm/tiered-isolation.bats` | Start broker; keep s110 stderr |

**Does not weaken** fail-closed clipboard/subscription semantics.

---

## Verification

| Check | Result |
| --- | --- |
| Unit (clipboard transfer/receive, handoff, control-plane identity) | **85 passed** |
| s110 solo on preserved VM | **14/0 PASS** |
| s57 solo after broker patch | **13/0 PASS** |
| Full `QCI_JOBS=1 ./ci/bin/qci bats --file …tiered-isolation.bats` | Not re-run end-to-end here: concurrent full QCI still holding host; golden rebuild hit tar/provision flake once. Priority probes validated on preserved disk. |

---

## Residual risk

1. **Tier-5 nested virt** still flaky when host suspends under multi-VM full runs — operational, not a clipboard security hole.
2. **Outer QGA death** after thrash still cascades 28–32 until suite/VM recovery improves.
3. **s57** still needs the broker package with the RevokeAllForUid allowlist in the golden image (source fix ships here; bake picks it up on next golden).
4. Host still runs concurrent full-QCI — re-run full bats suite when quieter for belt-and-braces.

## Bottom line

- **No ClipboardGate / broker default-deny fail-open** in this failure set.
- **One real product fix:** allow live admin `python3 -c` peers to call `RevokeAllForUid` (parity with `DecideRequest`).
- **Probe hardening** so empty dbus replies are not sold as FAIL-OPEN and survive brief suspend/NoReply.
