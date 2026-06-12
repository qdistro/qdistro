# Threat model

qdistro aims for brokered chokepoint provenance and contamination control, not
full fine-grained information-flow noninterference. Malicious apps are not
trusted to self-report security-relevant lineage. Authoritative lineage and
audit identity must come from broker decisions, kernel peer credentials,
SELinux labels, compositor endpoint identity, process snapshots, resource
handles, or workflow records.

qdistro's threat model is **explicit** because many design decisions
(hide-UI vs sandbox, cooperative vs enforced read-only, single-fingerprint vs
per-user auth) only make sense once the model is clear.

## In scope

1. **Accidental cross-silo data leaks.** A user working in `work-user` should
 not accidentally paste a password into `personal-user`'s browser. Clipboard,
 file-chooser, device-access, and window-handoff flows must make silo identity
 clear and gate cross-silo transfers.

2. **Containment of a compromised app.** An attacker who compromises one app
 (Firefox, an IDE, etc.) should be limited to the uid / container / VM that
 app runs in. They should not reach other silos' files, clipboards, or device
 streams.

3. **User overload.** Typical OS user-management UIs (Wi-Fi pickers, display
 settings, Bluetooth pairing dialogs) are not shown to regular users. Only
 admin sees hardware-configuration surfaces. Keeps cognitive load low and
 reduces misuse.

4. **Clear authorship chain for approvals.** Every cross-silo action has a
 polkit trail: who asked, when, what scope admin granted. Admin can review
 and revoke.

## Out of scope

1. **Fully adversarial user sessions.** A regular user session is *not*
 assumed to be actively malicious. If a user runs untrusted code that
 aggressively tries to escape its uid / container, qdistro does not guarantee
 containment unless that code runs in a VM tier. Defence against a hostile
 *session* requires VMs.

2. **Adversarial app authors targeting qdistro specifically.** First-party apps
 honour the `qdistro_app` SDK cooperatively. Read-only enforcement is a
 cooperative D-Bus flag, not a kernel-enforced sandbox. This is fine for
 apps the user wrote or trusts; it is not a defence against malicious apps.

3. **Hardware-level attacks.** Evil-maid, cold-boot, firmware implants,
 side-channels on the fingerprint reader — out of scope.

4. **Detection evasion for malicious purposes.** qdistro's logging and admin-
 visible activity indicators are for user awareness, not forensic resistance.

5. **Multi-tenant use.** Multiple humans using the same machine — not a goal.
 One fingerprint, one person.

## Consequences of this model

- **Hide-UI is often sufficient** where a paranoid model would require
 kernel-enforced sandboxing. Users don't accidentally misuse what they can't
 find.
- **Cooperative contracts** (read-only flag, SDK handoff and clipboard hooks)
 are acceptable for first-party apps. Third-party apps get the coarser
 tier-2 enforcement (input filter at the nested-compositor layer, container
 isolation).
- **Admin is the trusted computing base.** Admin's compositor, session manager,
 polkit agent, and locker all run with full trust. If admin is compromised,
 the whole machine is.
- **VMs exist for the real adversarial case.** The VM tiers on the
 [isolation ladder](isolation-tiers.md) are where containment hardens to
 actually-adversarial levels.
- **Untrusted update code runs in empty rooms.** Vendor auto-updaters, npm
 postinstall scripts, and unsigned plugin installers execute only inside
 template candidate builds, which contain no secrets, credentials, or user
 data by construction ([templates.md](templates.md)). They can poison the
 candidate — the audit gate's job — but cannot reach user documents, live
 sessions, or credentials: none exist there. The narrow residual
 is first-launch state migration, which runs in the real silo under a
 declared network policy with a pre-migration snapshot.

## The host kernel API is not a secure surface

Isolation tiers 0–3 (none / SELinux / podman / uid) all rest on the host
kernel: one kernel LPE collapses them together. Netfilter-class
*correctness* bugs (e.g. the iptables `!` rule mismatch) are a distinct
failure class — the rule silently does not mean what its author thinks —
and no amount of host hardening addresses it; only relocating the parsing
does.

Consequences:

- Tiers 0–3 are honest about what they are: graduated *authority and
 code* confinement sharing host-kernel fate. The VM tiers are where
 containment survives a host-kernel-quality bug.
- Hardware-facing parsing (802.11 frames, Bluetooth GATT, USB
 descriptors, IPP) migrates off the host into **device silos**
 ([device-silos.md](device-silos.md)) as those land — printing first
 ([printing.md](printing.md)), network next
 ([networking.md](networking.md)).

## TCB process discipline

The TCB processes (broker, session manager, locker, polkit agent) must
follow two dom0-style rules, enforced per-process (the admin *uid* keeps
NetworkManager until the net VM lands). Rule 1 is now
**shipped-with-exceptions**, not merely aspirational: the broker pilot
landed with VM-verified SELinux `neverallow` coverage and a systemd
runtime negative (`EAFNOSUPPORT`). The polkit agent and qdlocker also
ship systemd runtime no-network directives plus host tripwires, but
still need the SELinux `neverallow`/VM-negative half. The session
manager remains an explicit exception while it owns the netvm
ubus-over-HTTP control client (`session_manager/qdistro_netvm_client.py`).
Release tracking lives in `todo/fable-release/02-security-gate.md` S5.
Do not treat the broker pilot as evidence for those other domains.

1. **No network access.** systemd `PrivateNetwork=yes` (or
 `IPAddressDeny=any` + `RestrictAddressFamilies=AF_UNIX AF_NETLINK`)
 on the unit, AND no inet-socket permissions in the process's SELinux
 domain, backed by a `neverallow` assertion so the policy *build*
 fails if a future module grants it (the tier-2 policy already uses
 `neverallow` on network socket classes — same pattern). Precision
 matters per process: `IPAddressDeny` covers IP traffic only, and
 AF_UNIX / AF_VSOCK access is granted or denied separately — the
 implementation names exact SELinux domains and socket classes,
 including whether each process may use vsock. This does not shrink
 the D-Bus inbound surface; it eliminates the "privileged code
 fetches and parses something from the network" class and kills
 exfiltration after a TCB compromise.
2. **Envelope-only parsing.** TCB processes parse only size-capped
 structured envelopes (JSON / D-Bus metadata) with identity taken from
 kernel peer credentials, never from the payload. Anything richer —
 HTML, images, archives — is parsed in an unprivileged worker. The
 broker's `PageExtract` gate is the existing example of the pattern.

## Residual risk register

- **qdwin output-manager pre-shell mutation:** before qdshell binds,
  `zwlr_output_manager_v1` apply/test mutation is admitted for
  `allowed_uid`, and for the explicit test posture `allowed_uid == -1`;
  after qdshell binds, mutation is shell-only by client or same pid+uid.
  The decision is **snapshotted at manager-bind time** (`mgr->may_mutate`)
  and inherited by configuration objects, so a manager that bound during
  the pre-shell window (under `allowed_uid`, or under the `-1` open
  posture) **retains** mutation capability for the life of the resource
  even after qdshell later binds. The per-call policy is pinned by
  `qdwin_om_mutation_allowed()` in `qdwin-logic-unit`; the bind-time
  snapshot (`mgr->may_mutate`) and config inheritance that produce the
  persistence are pinned by the `output-manager-gate` source-invariant
  test (`qdwin/test_output_manager_gate.py`).
- **qdwin secctx helper root-launcher attestation:** when the helper's
  direct parent is verified uid 0 and has stable `/proc` starttime, an
  unreadable parent `/proc/<pid>/exe` basename is accepted because root
  launcher executables can be structurally unreadable to unprivileged
  qdwin. Pinned by `qdwin_secctx_root_launcher_attested()` in
  `qdwin-logic-unit`.
- **qdwin layer-shell pre-shell bind:** before qdshell binds,
  `zwlr_layer_shell_v1` may bind for `allowed_uid`; after qdshell binds,
  the shell client or same pid+uid path is required. Pinned by
  `qdwin_layershell_pre_shell_uid_allowed()` in `qdwin-logic-unit`.
- **qdwin `ext_idle_notifier_v1` ungated:** silo clients may bind the idle
  notifier and observe seat idle/resume transitions — a low-severity cross-
  silo presence/activity side-channel (no input contents, no pixels). Out of
  the S1 capture-gate scope and not yet gated; tracked for a future
  per-class visibility row in `qdwin_global_visible`. Not yet pinned.
- **qdwin `zwlr_output_manager_v1` enumeration ungated:** every client (any
  silo) may bind the output manager to enumerate head names / modes /
  geometry. Only the *mutation* path (apply/test) is gated (see the
  pre-shell mutation entry above); enumeration is intentionally open so any
  tool can read the display layout. This leaks display topology across
  silos — the same low-severity metadata side-channel class as
  `ext_idle_notifier_v1`, no input contents or pixels. Not gated; tracked
  with the same future per-class visibility work.
- **qdwin global-filter classify default-ORDINARY:** the `qdwin_global_visible`
  matrix is fail-closed for an unknown *kind*, but the live filter feeds it
  `qdwin_classify_global`, which returns `QDWIN_GLOBAL_ORDINARY` (visible to
  every client) for any global it does not recognise by pointer identity. A
  **new privileged libweston global therefore fails OPEN** until given an
  explicit classify row — the S1 gate covers the five enumerated kinds, not a
  whole-inventory enumeration. The remediation is the advertised-global
  inventory sweep tracked in `todo/fable-release` 02-security-gate.md S1
  (one classify row per advertised global). Not yet pinned.
