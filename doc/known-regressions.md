# Known regressions

The running ledger of **confirmed bugs that exist in a released build**, so users
and maintainers can tell at a glance whether a problem is already understood. This
is intake-facing — see [support.md](support.md) for how to report — and pairs
with the per-component issue tracker, not a replacement for it.

## Discipline

The ledger only earns trust if it is maintained honestly. The rules:

1. **An entry is added when a regression is confirmed in a released build** —
   reproduced, scoped, and (ideally) linked to a tracker issue. Not for
   speculation, not for unreleased work-in-progress.
2. **An entry names a workaround** when one exists, and the **severity** (sev-high
   = data loss / isolation breach / unbootable; sev-med = feature broken with a
   workaround; sev-low = cosmetic / metadata).
3. **An entry is retired to _Resolved_ when the fix is released** (not merely
   merged), with the fixing commit/manifest recorded. Resolved entries are kept,
   not deleted — the history is the point.
4. **Isolation/security regressions are sev-high by default** and also follow the
   private-disclosure path in [support.md](support.md) before they land here.
5. This ledger tracks *regressions and known bugs in shipped builds*. Capabilities
   that are **deferred by design** (not yet built) belong in the
   [feature scope](https://qdistro.org/status/) and the threat-model
   [deferred-by-design ledger](threat-model.md), not here.

## Open regressions

_None tracked against the v1 scope._ v1 has not shipped a signed release build
yet; this section is seeded empty and is the post-release intake surface
(release-checklist item 15). The first confirmed regression in a released build
lands here.

## Resolved (historical)

Pre-v1 install/doc regressions and gaps found and fixed during the release run,
kept for history. Full detail is in [recovery.md](recovery.md) and the release
trackers.

| Area | Regression | Resolution |
|---|---|---|
| Recovery (tty4) | Passwordless admin LXQt+labwc autologin was enabled on hardened installs where its stack was never installed → `203/EXEC` restart loop + greeter-bypass exposure. | tty4 fallback enabled only under the `dev` profile when the LXQt stack is present; daily-driver/release ship it installed-but-disabled and steer recovery to GRUB. |
| Recovery (text login) | `architecture.md`/`sessions.md`/`devices.md` documented a tty2 `tuigreet` text login that was never implemented. | Docs reconciled to reality: tty3 is the only interactive qdistro login; fully-broken-Wayland recovery is via the GRUB rescue/emergency target. |
| Snapshots (rollback CLI) | The per-user "roll back this user (full)" CLI (`qdistro-snap-swap`) shipped only via the VM-only templates installer, so it was missing wherever the snapshot feature itself landed. | Now installed from the `snapshots` bootstrap chain step, so it lands with the snapshot feature and the admin Snapshots panel. |
