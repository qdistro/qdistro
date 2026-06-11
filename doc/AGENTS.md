# Rules for contributors (humans and LLM agents)

This file is read at the start of every contributor session. The
rules below are not style preferences — each one is load-bearing for
qdistro's design or cost real time to learn during development.

## Project shape

qdistro is three repositories:

- **qdistro** (this repo) — broker, polkit agent, SDK, daemons,
  admin app, helpers, scripts, integration tests, documentation.
- **qdwin** — the libweston shell plugin. C, single-uid filtered
  global, no policy logic.
- **qdshell** — the desktop shell, Quickshell/QML. Forked from
  Noctalia.

Most contributor work lands in this umbrella. Touch qdwin only when
changing the compositor protocol or the libweston binding; touch
qdshell only when changing the visible desktop.

## Language policy

This is qdistro's most consequential rule.

- **Userspace is Python + Qt + QML.** Apps, the shell, the session
  manager, admin tools, SDK, daemons that talk over D-Bus. LLMs and
  humans modify these directly with no build-debug-rebuild cycle.
- **Bash for glue.** VM driver scripts (`scripts/vm/`), image and
  container build steps, bats integration tests. No policy logic in
  shell — anything that makes a security decision is Python behind
  D-Bus.
- **C is acceptable only in the TCB.** That's qdwin (the
  compositor) and a small set of protocol-glue daemons in
  `daemons/`. Adding a new C component to the umbrella requires a
  written justification.
- **When extending C-based infrastructure, use the embedded
  extension language the host already offers** rather than writing
  more C. Example: Weston 15's lua-shell drives rule-based window
  management from a runtime Lua script (a demo tiling shell ships
  with it). The script is as transparent to agents as Python or QML,
  and the C host stays untouched.
- **Thin C++ may become necessary for some Qt 6 integration**
  (custom QML types, Qt APIs without Python bindings). Keep it
  bindings-only — thin C++ glue, thick Python/QML logic on top, no
  product behaviour in C++ — and justify it in writing like new C.
- **No other general-purpose languages** (JavaScript, Go, …) in new
  code unless the existing ecosystem forces it (e.g. Electron
  webview content in third-party apps, or a Quickshell QML file
  with embedded JS that Quickshell expects). Every added language
  multiplies contributor context-switch cost.

The policy covers tests as much as product code: pytest for
Python, bats for shell, markdown playbooks for GUI scenarios (see
[Testing patterns](#testing-patterns)). A test an agent can't read
and extend confidently is as much a liability as opaque product
code.

The rationale is in [overview.md](overview.md) and
[compositor.md](compositor.md). The short form: if an LLM can't
modify a file confidently from the source alone, it shouldn't be in
that file's language.

## Single-tenant assumption

qdistro is a one-physical-person system. Multiple uids and silos exist to
separate data, state, authority, and work contexts, not to authenticate
different humans. When in doubt:

- The admin uid (`jan` on dev VMs, uid 1000) owns hardware and
  approves cross-uid actions.
- Regular user uids are implementation identities for silos and sessions
  spawned by admin's session manager.
- There is no multi-user login screen, no per-user fingerprint, no
  per-user network stack. One human, one boot.

Design proposals that assume "two human users" are out of scope.

## Linux-only

qdistro is Linux-only. There is no Windows guest support, no macOS
runtime support, no Android shell port. The previous design briefly
considered Windows guests via FreeRDP; that scope was dropped.
Removing it kept the protocol surface small enough for one author.

## Wayland-only sessions

Test VMs run Wayland sessions. Xorg + xfce was removed from the
bootstrap. XWayland is kept as a pragmatic compatibility bridge for
apps that don't yet speak Wayland (and for tests that drive
xdotool, which cannot inject under labwc / qdwin without an X11
bridge).

## D-Bus over ad-hoc IPC

Every cross-process interface in qdistro is D-Bus. New daemons must
publish a D-Bus interface, not a raw Unix socket protocol. Excep-
tions: the compositor's wp_security_context_v1 listener, browser
native messaging (the browser's protocol, not ours).

The broker uses **dbus-broker** (the message broker daemon), not
the older dbus-daemon. Two specific landmines from past sessions:

- **`dbus.service.BusName` must be anchored.** dbus-python releases
  the name on GC; bind it to a local or attribute that outlives the
  mainloop.
- **Cross-uid session-bus access is impossible on dbus-broker.**
  Root cannot connect to `/run/user/<uid>/bus`. Use per-uid
  system-bus well-known names that the target service claims itself.

See [permissions.md](permissions.md) for the broker shape and
[qbus.md](qbus.md) for the per-concern split.

## Default-deny is the only safe default

Any new cross-uid or cross-tier capability ships with broker policy
enforcement from the first commit. The broker is the single arbiter
of cross-uid actions; bypassing it punches a hole in the isolation
model.

Defaults:

- New permission scope → default-deny, require admin approval.
- New cross-uid signal → broker mediation, audit log entry per
  decision.
- New daemon → SELinux policy module added to `selinux/`. Permissive
  is acceptable during initial development; enforcing must be
  reached before the daemon ships.

## Testing patterns

- **pytest** for Python — under `tests/unit/`. Headless, no D-Bus,
  no display. Should run in under 5 seconds. Use Textual `Pilot`
  for TUI snapshot tests; mock D-Bus services with the in-process
  fakes already present.
- **bats** for shell — under `tests/integration/vm/`. Always run
  inside a VM. Each bats file is a topic; new scenarios add a
  `@test` entry and assert via the helpers in `helpers.bash`.
- **Markdown playbooks** for GUI scenarios — under
  `tests/integration/permissions-gui/` and
  `tests/integration/qdwin-noctalia/`. Each numbered `NN-*.md` is
  one scenario, executed by a graphic-aware test runner (human or
  LLM) following the playbook step by step.

GUI tests MUST run inside a VM. Bad input injection on the host has
killed prior development sessions by closing the developer's
terminal. This is a hard rule.

## VM workflow

Test VMs are built from upstream OpenSUSE Tumbleweed JeOS via
`scripts/vm/build-baseweed-from-scratch.sh` (one-time, ~5 minutes)
+ `scripts/vm/build-baked-baseweed.sh` (bake project dependencies,
re-runnable, ~76 seconds when cached). Test clones come from
`clone-baseweed.sh` (instant, copy-on-write). The whole pipeline is
chained by `scripts/vm/spin-test-vm.sh`.

Driver tools live in `scripts/vm/`:

- `vm-exec <name> <cmd>` — run a shell command in the VM via
  qemu-guest-agent. Beware: embedded `"` characters break the JSON;
  use single quotes or push a script file.
- `vm-gui <name> <action>` — input injection via xdotool through
  XWayland. Modifier chords (Ctrl+, Alt+) are unreliable under
  labwc; use `virsh send-key --codeset linux` for those.
- `vm-start-and-wait <name>` — boot and block until ssh is ready.

VM names must end with a `YYMMDD-HHMM` timestamp suffix to avoid
collisions across parallel test runs.

The standard test password is `kruger`. The standard admin uid
is `jan` (uid 1000). The spec calls the admin role "admin" but the
actual OS account is `jan`.

## What not to do

- **Don't mock the database in integration tests.** A prior
  incident had mocked tests passing while the prod migration failed.
  Integration tests hit a real database (typically the sqlite cache
  inside the VM).
- **Don't add fallbacks or validation for impossible states.**
  Trust internal code and framework guarantees. Validate at
  trust-boundary entry points (user input, external APIs, D-Bus
  method handlers).
- **Don't add comments that re-state the code.** Comments are for
  *why* — non-obvious invariants, workarounds for bugs, surprising
  behaviour. If removing the comment doesn't confuse a future
  reader, don't write it.
- **Don't write planning documents.** No `tasks/`, no `todo/`, no
  `chat-2026-MM-DD.md` files. Git log + commit message + this `doc/`
  set is the record. If a design decision needs preserving,
  the commit body or a `doc/*.md` edit is the right place.
- **Don't add feature flags or backward-compat shims** when you can
  just change the code. qdistro is pre-release; renames and breaking
  changes are cheap.

## Commit conventions

- Subject in imperative mood, <72 chars, lowercase area prefix:
  `broker: rate-limit per-(uid, action)`, `pwd: PCR-bound TPM seal`,
  `doc: clarify isolation-tier boundary`.
- Body explains *why*. The diff shows *what*. Two-line bodies are
  fine; one-paragraph bodies are common; multi-paragraph bodies for
  load-bearing decisions are encouraged.
- One commit per logical change. Don't bundle a refactor with a
  feature. Don't squash unrelated fixes.
- No "WIP", no "fix typo", no "address review comments" — rebase
  those out before pushing.

## What this AGENTS.md is NOT

This is not a coding-style guide (variable naming, line length).
That lives in [dev.md](dev.md) for the Python side, in the QML
file conventions for qdshell, and in `qdwin/doc/AGENTS.md` for
qdwin. This file is the project-wide invariants — read it before
your first contribution to any qdistro repo, then defer to the
repo-specific docs for code-level rules.
