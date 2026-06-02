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
