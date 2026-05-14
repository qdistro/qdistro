# GUI test runner — for graphic-aware subagents

Two roles collaborate on GUI testing:

- **Orchestrator** — the parent agent that picks scenarios, spawns
 runners, aggregates results. That's usually whoever is running the
 work for the user in this repo.
- **Runner** — a graphic-aware subagent spawned for a single scenario.
 Reads one `NN-*.md` scenario file, executes it end-to-end against a
 live VM, returns a PASS/FAIL report.

This document covers both roles. Start from the section that matches
what you're doing; read the other when you need to understand the
handoff.

---

## As the runner

You are running a GUI acceptance test for qdistro. Each `NN-*.md` file
in this directory is a **user-authored scenario**: prose describing a
sequence of setup steps, actions, and visual assertions you verify from
screenshots. You do not write or modify scenarios — you execute them
and return a report.

## Environment

- Host: openSUSE Tumbleweed with `libvirt` + `virsh`.
- Target: a running virt-manager VM (name passed via `$VMNAME`, default
 picked from `virsh list --name --state-running | head -1`).
- VM runs LXQt + labwc on tty3 as user `admin`, autologged via greetd.
- `qdistro-admin-broker.service` is active under root; TUI at
 `/usr/local/bin/qdistro-admin-tui`; Qt admin app at
 `/home/admin/qdistro/admin_app/qdistro_admin_app.py`.
- Tools on host (absolute paths — not on PATH):
 - `${QDISTRO_REPO}/scripts/vm/vm-exec <vm> '<cmd>'`
 runs shell as root in VM via qemu-guest-agent.
 - `${QDISTRO_REPO}/scripts/vm/vm-gui <vm> ...`
 `start`, `screenshot [file]`, `click x y`, `type <text>`,
 `key <keysym>` — implemented via virsh screenshot + xdotool in
 XWayland (labwc Wayland; `DISPLAY=:0` XWayland is available).
- VM user password is `$QDISTRO_VM_PASSWORD` for `admin` and `work`.

## Hard-learned pitfalls (read before running commands)

1. **vm-exec quoting is fragile.** vm-exec builds qemu-ga's JSON
 payload with string concatenation; any embedded `"` breaks the
 parse with `failed to parse JSON: array value separator ',' expected`.
 Single quotes alone are fine; the trap is `sqlite3 DB "DELETE FROM
 ..."` where the inner `"..."` groups the SQL as one argument.
 Two working patterns:

 a) **Base64-wrap whole scripts** with embedded quotes:
 ```bash
 B64=$(base64 -w0 <<'EOF'
 <your multi-line script here, quotes OK>
 EOF
 )
 vm-exec $VM "echo $B64 | base64 -d | bash"
 ```

 b) **Base64 just the SQL** — concise for one-shot DELETEs used in
 Setup/Teardown across the scenarios:
 ```bash
 SQL_B64=$(base64 -w0 <<'SQL_EOF'
 DELETE FROM approvals WHERE action='test.action';
 SQL_EOF
 )
 vm-exec $VM "echo $SQL_B64 | base64 -d | sqlite3 /path/to.sqlite"
 ```
 The vm-exec payload is now quote-free (`echo <b64> | base64 -d |
 sqlite3 <path>`) and JSON-encodes cleanly. Scenarios 04 / 06 / 07 /
 10 use this pattern — copy it verbatim when you need SQL in Setup.
2. **Backgrounded GUI processes from `vm-exec` die when the agent call
 returns** unless you detach them. Use `setsid ... </dev/null
 >/tmp/foo.log 2>&1 &` inside a script.

 **Launcher env caveat**: `runuser -u admin -- env DISPLAY=:0 /tmp/launch.sh`
 does NOT inherit admin's Wayland-session env (`XDG_RUNTIME_DIR`,
 `DBUS_SESSION_BUS_ADDRESS`, `WAYLAND_DISPLAY`). qterminal, the Qt
 admin app, and anything D-Bus-session-aware will abort on connect.
 The repo ships two launchers that DO set the env correctly — use
 these instead of hand-rolling `runuser -u admin -- env ...`:

 - `/usr/local/bin/qdistro-start-admin-app` — launches the PyQt6
 admin approval app.
 - `/usr/local/bin/qdistro-start-admin-tui` — launches qterminal
 wrapping the Textual TUI (`qdistro-admin-tui`).

 Source in `deploy/start-admin-{app,tui}.sh`.
3. **Wayland input injection is unreliable on labwc.** Drive the GUI
 via keyboard (`vm-gui key`) through XWayland where possible; do
 **not** click pixel coordinates for Wayland-native windows unless a
 scenario explicitly instructs to.

 **Caveat for modifier-key shortcuts on XWayland Qt apps** (e.g.
 the admin approval app's `Ctrl+Y`/`Ctrl+N`): `vm-gui key ctrl+n`
 silently fails to reach the app — xdotool/XTest routes modifier
 combos through XWayland's focus handling, which labwc does not
 synchronize with the Qt window's X input focus. Use the KVM
 keyboard instead:
 ```bash
 virsh send-key "$VM" --codeset linux KEY_LEFTCTRL KEY_N
 ```
 This works because `virsh send-key` injects at the virtual
 keyboard (evdev) layer, below X/Wayland entirely.

 **Plain-key caveat too**: even single letters / digits through
 `vm-gui key <X>` silently no-op against qterminal under labwc in
 practice — xdotool reaches the X server but the focused terminal
 window doesn't see the key consistently. TUI scenarios (01, 02,
 05, 09) drive every plain key (`d`, `1`, `2`, `3`, `r`, `?`,
 `Escape`) via `virsh send-key` for the same reason modifier
 chords do. If a future scenario finds `vm-gui key` works on a
 focused qterminal — it sometimes does — that's fine, but don't
 rely on it; `virsh send-key` is the portable path.

3b. **Mouse-click scenarios describe targets by visible text, not
 pixels.** When a scenario says `click Approve`, `click the "1
 hour" radio`, or `click OK`, the runner's job is:
 1. Take a fresh screenshot after the window is focused.
 2. Extract text + bounding boxes from the screenshot. A
 vision-capable LLM agent can read text off the PNG
 directly; otherwise run `tesseract` (once installed —
 neither host nor VM ship it by default:
 `zypper -n install tesseract-ocr tesseract-ocr-traineddata-english`).
 Invocation:
 `tesseract /tmp/foo.png - -c tessedit_create_tsv=1` yields
 tab-separated `(text, left, top, width, height)` rows
 you can parse straight into click targets.
 3. Find the bounding box for the exact text the scenario
 names. If the text is a label beside a control (radios,
 checkboxes), the clickable glyph sits ~15 pixels to the
 left of the label at the label's vertical midpoint —
 click there, not on the label itself.
 4. For buttons (`Approve` / `Deny` / `OK` / `Cancel`), click
 the center of the text's bounding box; Qt buttons are
 hit-targets larger than their label.
 5. Post-click screenshot; verify state via OCR again (the
 expected text disappeared, a new label appeared, etc.).
 **Never hard-code pixel coordinates in a scenario.** Qt
 font/DPI/theme drift invalidates pixel offsets across clones;
 OCR-on-text keeps the scenario portable. Likewise, scenario
 authors write target labels using the exact string Qt renders
 (match the source code), so OCR noise is minimised.
 If OCR can't find the named text, don't fuzzy-guess — report
 FAIL with the screenshot attached and the OCR output as
 justification. A scenario whose target text isn't on screen
 is either describing a different UI state (scenario bug) or
 the application actually failed to render (app bug); both
 want the human's eyes.
4. **The TUI has no menubar of its own.** Its surrounding chrome
 (titlebar "Shell No. 1", menubar, tab strip) belongs to qterminal
 and is not under test. Assertions about the TUI refer to the inner
 content area only.
5. **Don't `zypper dup`** — it restarts qemu-guest-agent and orphans
 your in-flight `vm-exec` calls. Stick to `zypper -n install` for
 additions.
6. **Seed state before asserting.** Scenarios that need a pending
 request must trigger it first (usually by running
 `qdistro-test-permission` as `work` in the background — see
 `deploy/step-d-test.sh` for the pattern).
7. **qterminal default geometry is pinned to 1200×700** via
 `/home/admin/.config/qterminal.org/qterminal.ini` (`[MainWindow]
 size=@Size(1200 700)`). This width is needed so the TUI header
 subtitle (`• scope: ...`) and full footer (incl. `4`, `5`, `r`,
 `? Help`, `q Quit`) fit without truncation. If a scenario is
 hitting truncation, verify the ini still has that size and
 re-launch qterminal; don't rewrite the scenario to accept
 truncation.
8. **Clean up TUI processes in teardown.** `pkill -u admin -f
 qdistro_admin_tui` — closing qterminal does *not* always reap the
 TUI subprocess. Stale TUIs accumulate across runs and hold
 broker connections open.
9. **Broker state persists across scenarios.** A pending request
 left by a prior run (e.g. a scenario that failed before its
 deny/approve step) will show up as non-empty state in the next
 scenario's S1. If your scenario asserts an *empty* starting
 state, drain the broker in Setup via
 `systemctl restart qdistro-admin-broker.service`.
10. **Scenarios are NOT safe to run concurrently on one VM.**
 Every Setup block restarts the broker and kills the admin
 app; two runners overlapping will drain each other's state
 mid-flight and produce spurious FAILs. The parent agent
 orchestrating a batch must serialize: one scenario at a
 time against a given VM. If you need wall-clock parallelism,
 spin up a second clone from baseweed per memory's cloning
 procedure — one runner per VM.

## Running a scenario

1. Pick the scenario file from the argument (e.g.
 `01-tui-approver-visual.md`). Read it top to bottom before acting.
2. Follow the **Setup** block verbatim. Stop and report failure if any
 setup step exits non-zero or a prerequisite is missing.
3. Execute the **Steps** in order. For each "screenshot X.png" step,
 take a screenshot into `/tmp/<scenario-stem>-<step-name>.png` and
 **Read it** so you can reason about pixels.
4. For each **Assert** bullet, decide PASS or FAIL by looking at the
 most recent screenshot. Be concrete — quote the region of the
 screenshot that informed the decision ("top-left of content area
 shows bold white text 'qdistro admin approvals (TUI)' on dark
 background").
5. Tear down per the **Teardown** block even on failure.

## Report format

Return a single markdown block:

```
# <scenario filename> — <PASS | FAIL | ERROR>

<when ERROR: one sentence what went wrong in setup/teardown>

## Assertions
- [PASS|FAIL] <assertion text> — <one-line justification referencing screenshot>
- ...

## Screenshots
- /tmp/<scenario-stem>-<step-name>.png — <one-line description>
- ...
```

Keep the report under 400 words. Do not include full screenshot
binaries; just paths.

## Things that are NOT your job

- Editing scenarios, the TUI, broker, or any repo files.
- Fixing bugs you find. Report them in the FAIL justification; the
 parent agent decides what to do.
- Running long exploratory investigations. If a scenario step is
 ambiguous, report ERROR with the ambiguity — don't guess.

---

## As the orchestrator

You're the parent agent. You've decided a set of scenarios needs to be
executed, and you'll spawn runner subagents to do it.

### Preconditions

- The target VM is running, bootstrapped, and in a known-drained state
 (broker restarted, approvals cache empty, no admin app lingering, no
 stale `qdistro-test-permission` or `dbus-send` processes as `work`).
 Runners assume this and their Setup blocks may not fully recover
 from a polluted starting state — especially scenarios 04, 07, 10
 which poke the cache.
- The host has `virsh` and the `scripts/vm/` tools accessible.
- If you need tesseract, it's NOT installed by default on host or VM —
 the runner will use vision-direct on the screenshots instead
 (Claude is vision-capable; Read of a PNG surfaces the text).

### Spawn pattern (validated)

Use the Agent tool with `subagent_type=general-purpose`. The prompt
must be self-contained — the runner starts with no conversation
history. Include:

1. The absolute path to the scenario file.
2. An instruction to read `tests/integration/permissions-gui/AGENTS.md` first.
3. The VM name.
4. A one-paragraph summary of current VM state (helps the runner
 decide whether to trust Setup or recover first).
5. Tool paths (the `scripts/vm/` binaries aren't on `$PATH`).
6. The report format you want back.

Template (fill the `<...>` placeholders):

```
You are a graphic-aware UI test runner for qdistro. Execute a single
GUI scenario end-to-end against a live VM and return a PASS/FAIL report.

## Scenario
<absolute path to NN-*.md>

## Orientation — READ FIRST
Read ${QDISTRO_REPO}/tests/integration/permissions-gui/AGENTS.md top to bottom.
Internalise (modifier-key chords via `virsh send-key`), (OCR-first
click targeting), (base64 wrapping for scripts with quotes).

## VM
VMNAME=<vm>

<one paragraph on current VM state: drained or not, deployed,
anything the runner should expect to find>

## Tools
- ${QDISTRO_REPO}/scripts/vm/{vm-exec,vm-gui} — absolute paths.
- vm-gui <VM> screenshot /tmp/<name>.png then Read the PNG (vision-capable).
- vm-gui <VM> click X Y — OCR the label, click its bounding-box center.
- virsh send-key <VM> --codeset linux KEY_... — modifier chords.
- Base64 scripts with embedded quotes.

## Execution contract
1. Execute Setup, Steps, Teardown exactly as the file says.
2. Each Assert bullet → PASS or FAIL with concrete justification.
3. Tear down even on failure.
4. Do NOT modify repo files. Do NOT fix bugs — report them.

## Report format
[paste the report block from this file's `## Report format` section]

Keep report under 500 words.
```

Run the spawned agent with `run_in_background: true` and wait for its
completion notification; don't poll the output file (it's a JSONL
transcript and will blow your context).

### Serialization

As noted in pitfall : **one runner per VM at a time.** Every
scenario's Setup restarts the broker and kills the admin app;
concurrent runners collide and produce spurious FAILs. For
wall-clock parallelism, clone multiple VMs from baseweed and
dispatch one scenario per VM.

### Between scenarios

After a runner finishes, drain the VM state before spawning the next:

```bash
B64=$(base64 -w0 <<'EOF'
pkill -u admin -f qdistro_admin_app 2>/dev/null || true
pkill -u admin -f qdistro_admin_tui 2>/dev/null || true
pkill -u work -f qdistro-test-permission 2>/dev/null || true
pkill -u work -f dbus-send 2>/dev/null || true
systemctl restart qdistro-admin-broker.service
sleep 2
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals;" 2>/dev/null || true
EOF
)
vm-exec "$VM" "echo $B64 | base64 -d | bash"
```

This matches what each scenario's Setup would do itself, but running
it centrally keeps weaker scenarios honest.

### Smoke subset (5-minute pre-push sweep)

The full GUI corpus is 49 scenarios. For a pre-push smoke pass, run the
five scenarios below — they cover the broker → compositor → shell
happy path end-to-end with ~5 min of orchestrator wall-clock per VM.

| Scenario | Why it's load-bearing |
|---|---|
| `tests/integration/permissions-gui/04-qt-admin-app-approve.md` | broker + Qt admin app + scope cache — the cross-cutting permission path that touches almost every component |
| `tests/integration/qdwin-noctalia/01-bar-visible.md` | qdshell renders at all; smoke-detects a fully-broken shell |
| `qdwin/tests/gui/04-alt-tab-switch.md` | switcher + focus + raise + close-and-refocus; the most-touched compositor code path |
| `qdwin/tests/gui/03-locker-cycle.md` | lock + unlock end-to-end; exercises the pwd vault binding |
| `qdwin/tests/gui/13-focus-events-emitted.md` | spawn / close / drop-to-no-window — the focus-event ground truth that downstream window-list highlight depends on |

Optional add-ons when you're touching the bar:

| Scenario | Why |
|---|---|
| `qdwin/tests/gui/12-bar-no-overdraw.md` | bar height == exclusion height; verifies the 1px-overdraw fix and the `exclusionZoneBleed` toggle round-trip |
| `qdwin/tests/gui/14-bar-content-quiet-when-idle.md` | journal stays quiet when idle; catches a returning remap storm |

Run them in series (`one VM at a time`) and drain state between scenarios
per the [Between scenarios](#between-scenarios) block. A green smoke is
not a green full pass — it just blocks the most expensive merge mistakes.

### Interpreting reports

- **PASS** — all Assert bullets passed; trust the runner.
- **FAIL** with anomalies flagging *scenario* pollution / ordering /
 environment drift — re-run serially after a drain. If it still FAILs,
 there's a real bug.
- **FAIL** with concrete app-state justifications (e.g. "S3 screenshot
 shows a new pending row, no cache hit") — that's a real finding;
 don't mask it. Either the scenario is stale, or the app regressed.
 Open a todo card with the runner's quote and fix one or the other.
- **ERROR** — the runner couldn't execute (missing prerequisite,
 ambiguous step). Fix the scenario or the environment, don't retry
 blindly.
