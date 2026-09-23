# Host-side qdwin GUI tests — for graphic-aware subagents

Two roles, same shape as `phase1/gui-tests/AGENTS.md`:

- **Orchestrator** — picks scenarios, spawns runners, aggregates.
- **Runner** — vision-capable subagent spawned per scenario; reads
  one `NN-*.md` file, executes it, returns PASS/FAIL.

This doc covers both. Read whichever applies. The other becomes
relevant when you handle a handoff.

## Why these are host-side, not VM-side

The `phase1/gui-tests/` suite runs against a labwc VM (Xorg-on-tty3,
xdotool through XWayland). qdwin is **Wayland-only and headless** in
this harness — no XWayland, no X server, no popups on the user's
desktop. weston runs with `--backend=headless`, so the test
compositor is invisible to the host (no window manager involvement,
no display-server contention).

User preference: tests must not pop windows on the host while they
work. The headless backend satisfies this; weston-screenshooter
captures qdwin's framebuffer directly.

## Environment

- Host: openSUSE Tumbleweed, weston 14.0.2 from distro packages.
- qdwin built once into `/tmp/qdwin-host-install/lib/weston/qdwin-shell.so`
  by the harness on first run (`tests/host/lib.sh:ht_require_build`).
  Rebuild via `ninja -C /tmp/qdwin-host-build install` (with
  `~/.local/bin` on PATH for meson).
- qdshell runs from the in-tree source (`qdshell shell.qml`).
- weston-screenshooter is the screenshot tool. `start.sh` explicitly
  enables qdwin's dev/test screenshooter gate with
  `QDWIN_ENABLE_SCREENSHOOTER=1`. weston-terminal is the default
  content-client used by scenarios; substitute via
  `--no-terminal` + manual launch if a scenario needs something else.

## Harness scripts

All under `tests/host/`. Each takes a `<test-id>` (use the
scenario file's stem, e.g. `01-max-restore`).

- **`start.sh <id> [opts]`** — provisions per-test state at
  `/tmp/qdwin-host-tests/<id>/`, spawns weston (headless) +
  weston-terminal + qdshell. `--colors UID=#hex,...` sets per-uid
  border colour map. `--no-terminal` / `--no-shell` skip the
  respective client. `--pipewire` additionally loads libweston's
  PipeWire **backend** and configures one pipewire output — needed by
  any scenario that expects `subscribe_view_stream` to be *approved*,
  since without a free pipewire output qdwin answers `denied "no free
  pipewire output"` and every stream-shaped assertion is vacuous. It
  requires a PipeWire daemon on the host (`/run/user/$UID/pipewire-0`)
  and exits 8 without one; the backend is pointed at the real user
  runtime dir via `PIPEWIRE_RUNTIME_DIR` while weston itself keeps the
  per-test `XDG_RUNTIME_DIR`. Echoes `<id>` on success.
- **`screenshot.sh <id> <name>`** — captures qdwin's output to
  `/tmp/qdwin-host-tests/<id>/shots/<name>.png` and echoes the path.
- **`ctrl.sh <id> <cmd> [args...]`** — sends a line to qdshell's
  ctrl-socket, prints the reply. Exits non-zero on `err` reply.
  Commands: `max <h>`, `restore <h>`, `min <h>`, `raise <h>`,
  `close <h>`, `state <h>`, `list`, `quit`.
- **`stop.sh <id> [--keep-logs]`** — kills processes; removes the
  per-test dir unless `--keep-logs`.

State layout under `/tmp/qdwin-host-tests/<id>/`:

```
runtime/         XDG_RUNTIME_DIR for weston + clients
weston.log       weston log (qdwin-shell.so output included)
qdshell.log      qdshell stderr
clients.log      weston-terminal + any other clients
ctrl.sock        qdshell --ctrl-socket
pids/<role>      one file per spawned process
shots/*.png      screenshots
```

## What "click" means here

Headless weston has no input backend. Until weston-test or
ext-virtual-pointer-v1 lands on Tumbleweed, scenarios drive shell
actions through **qdshell's ctrl-socket** rather than synthesising
real wl_pointer events. The agent's "click" is sent as a command
(`ctrl.sh <id> max 1` instead of "click the maximise button at (X,Y)").

This means scenarios test **the action's effect**, not **the click →
action wiring** (the wl_pointer routing in qdshell, see
`the architecture doc` § "Chrome and content independence").
The wiring is verified manually + will get scripted coverage when
real headless input becomes available.

The agent's vision job is unchanged: read the screenshot, identify
the visible state ("a terminal window with a cyan border on the left
edge", "the screen is now mostly black with no visible window"),
PASS/FAIL on what's actually there. Don't fuzzy-match. If a scenario
expects the window to fill the screen and the screenshot shows it at
small-window size, FAIL with the screenshot path attached.

## Hard-learned pitfalls

1. **`weston-screenshooter` writes to CWD with a fixed filename
   pattern** (`wayland-screenshot-YYYY-MM-DD_HH-MM-SS.png`); the
   `-o` flag isn't honoured in the Tumbleweed build. `screenshot.sh`
   wraps this — call it, don't invoke weston-screenshooter directly.
2. **wayland socket race.** `start.sh` waits for the socket file to
   appear (covers the bind→listen window). If a scenario backgrounds
   another wayland client, give it ≥0.5s before assuming it has bound.
3. **ctrl-socket timing.** `start.sh` waits for `ctrl.sock` too. If
   a script sends a command before qdshell has finished its registry
   roundtrip + bind_as_shell, `list` may return zero toplevels.
   Build a short retry into your assertion if you see flaky empty
   replies.
4. **One scenario per test-id at a time.** `start.sh` wipes
   `/tmp/qdwin-host-tests/<id>/` clean. Two runs with the same id
   step on each other.
5. **Screenshooter is double-gated.** qdwin only registers it when
   `QDWIN_ENABLE_SCREENSHOOTER=1` and the compositor euid matches
   `QDWIN_ALLOWED_UID`; weston also requires `--debug`.
   `start.sh` sets both for host tests. If you call weston manually
   for diagnostic purposes, remember both gates.
6. **Don't run as root.** XDG_RUNTIME_DIR ownership and weston's
   default seat handling assume a regular user uid.

## As the runner

Each `NN-*.md` is a user-authored scenario. Don't modify scenarios.
Execute as written.

1. Pick the scenario file from your assignment.
2. Run **Setup** verbatim. If any step exits non-zero, tear down and
   FAIL with that output.
3. Execute **Steps** in order. For each `screenshot N` or
   `screenshot.sh ... NAME` line, take the shot and **Read** the PNG
   so you can reason about pixels.
4. For each **Assert** bullet, decide PASS or FAIL by looking at the
   most recent screenshot. Quote the region that informed the
   decision ("top half is black background; cyan vertical strip
   visible at x≈0..8 from top to bottom; no terminal text visible").
5. Tear down per **Teardown** even on failure.

Report format (under 400 words):

```
# <scenario filename> — <PASS | FAIL | ERROR>

<one sentence per ERROR>

## Assertions
- [PASS|FAIL] <assertion text> — <one-line justification w/ screenshot path>

## Screenshots
- /tmp/qdwin-host-tests/<id>/shots/<name>.png — <one-line description>
```

## Things that are NOT your job

- Editing scenarios, qdshell, qdwin, harness scripts.
- Fixing bugs you find — report them in FAIL justification.
- Running long exploratory investigations. Ambiguous step → ERROR.

## As the orchestrator

You're spawning runners. The harness is concurrency-safe per-test-id
(each scenario gets its own /tmp/qdwin-host-tests/<id>/, its own
weston instance, its own wayland socket). You can run multiple
scenarios in parallel — just give each a distinct id (the scenario
filename stem is the convention).

Spawn pattern:

```
Agent({
  description: "Run NN-foo scenario",
  subagent_type: "general-purpose",
  prompt: """
    Execute tests/host/NN-foo.md against this repo.
    All scripts are in tests/host/. Treat the scenario as
    user-authored; don't modify it. Read each screenshot you take so
    you can reason about pixels. Return the standard report (<400 words).
    Pitfalls + harness contract: tests/host/AGENTS.md.
  """
})
```

If you're running a batch, parallel is fine — but if a scenario
needs a specific port (it shouldn't; the harness uses unix sockets
only), or shares a fixed file path with another, serialize.
