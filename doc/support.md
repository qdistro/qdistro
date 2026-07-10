# Support and reporting issues

Where to get help, where to report a bug, and how security reports are handled.
This is the operator-facing intake page; recovery procedures live in
[recovery.md](recovery.md) and the known-issue ledger in
[known-regressions.md](known-regressions.md).

## Before you file: self-service

Most "it won't boot / won't log in" situations are recoverable without a bug
report. Work through [recovery.md](recovery.md) first — tty3 production login,
the GRUB rescue/emergency target, read-only snapshot boot + `snapper rollback`,
per-user `qdistro-snap-swap`, and re-running the bootstrap with `--resume` /
`--rerun-step`. Then check [known-regressions.md](known-regressions.md) to see if
what you hit is already tracked.

## Where to report a bug

qdistro is many repositories, but issue intake is **centralized**:

> **File issues at the meta tracker:
> [codeberg.org/qdistro/qdistro/issues](https://codeberg.org/qdistro/qdistro/issues).**

Codeberg runs Forgejo, so this is a normal issue tracker. File everything there
even if you think the bug is in a component repo (`qdwin`, `qdshell`,
`qdlocker`, …) — maintainers move or label it to the right component. One front
door keeps users from having to know the repository split.

A useful report includes:

- **What you ran and what happened** — the single most useful line is
  "I ran step X and Y was unclear / broken."
- **Install profile** — `dev` (developer preview) or `daily-driver` (hardened
  release). They are different trust and code paths.
- **Release identity** — the release manifest version (or the commit/tag your
  bootstrap pinned). v1 names a *signed manifest*, not a single tag.
- **Relevant journal lines** — `journalctl -b` for the failing unit
  (e.g. `journalctl -u qdistro-admin-broker.service`, `-u greetd`,
  `-u 'qdistro-silo@*'`).
  Redact anything sensitive first.
- **Tier / silo** — which isolation tier and silo the problem occurred in.

Please **do not** paste vault contents, private keys, or full unredacted logs.

## Security issues — report privately

Do **not** open a public issue for a vulnerability. Security reports go through a
private channel so a fix can ship before the details are public.

> **Security contact:** `security@qdistro.org` *(placeholder — the published
> address/key is finalized at the v1 release; until then, mark a Codeberg issue
> confidential or contact a maintainer directly rather than posting details).*

What we consider in scope follows the [threat model](threat-model.md): accidental
cross-silo leaks, escape from a tier's confinement, broker/approval bypass,
TCB-process network or parsing surface. Out of scope (by design, see the threat
model): fully adversarial sessions outside a VM tier, hardware attacks, and
multi-tenant assumptions. When you report, say which tier/silo and whether the
issue needs a VM tier to matter.

For v1, maintainers intend to acknowledge security reports, coordinate a
disclosure timeline, and credit the reporter unless they ask otherwise.

## Severity, and what "known" means

Confirmed bugs that ship in a release are tracked in
[known-regressions.md](known-regressions.md) until a fix is released, so users can
see whether their problem is already understood. A bug being "known" is not the
same as "fixed" — the ledger states the workaround and the fixing change when one
exists.
