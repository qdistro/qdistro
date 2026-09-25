# Prompt template: qdistro GUI scenario runner

You are running one qdistro GUI scenario as part of local CI.

Inputs:

- VM name: supplied by the qci-generated prompt.
- Scenario file: supplied by the qci-generated prompt.
- Artifact directory: supplied by the qci-generated prompt.

Procedure:

1. Read the scenario file top to bottom.
2. Read the nearest `AGENTS.md` for that scenario directory.
3. Source the helper script documented by that `AGENTS.md`.
4. Execute Setup, Steps, Assertions, and Cleanup exactly once, serially.
5. Read the scenario's `<!-- qci:visual: required -->` or
   `<!-- qci:visual: none -->` declaration. On `required`, capture a
   screenshot after every GUI action that changes state, THROUGH YOUR
   LANE'S OWN CAPTURE TOOL, INTO the artifact directory. For the
   labwc/admin lane that is `vm-gui` (`screenshot`, `screenshot-fresh`,
   `click-preview`, `click-confirm`); for the qdwin lanes it is the
   `qdwin_screenshot` / `qdwin_apps_screenshot` helper your `AGENTS.md` names.
   Only frames the harness's own capture tool took are graded as visual
   evidence; an image produced any other way is not evidence. Never delete or overwrite a capture once it is in the
   artifact directory — including one that shows a failure. Removing it is
   detected and turns the verdict into ERROR; keeping it and reporting FAIL is
   the correct outcome. Capture `$VMNAME` and nothing else: a capture of any
   other VM is refused outright and fails the capture command.
   On `none`, required assertions are not pixel-dependent. Do not take
   screenshots as a substitute for the oracles the scenario names
   (D-Bus, sqlite, journal, exit code). A rejected, near-black, or
   missing screenshot is not ERROR and not FAIL.
6. **OPEN EVERY FRAME YOU INTEND TO ASSERT ON.** Before stating anything about
   what is on screen — a label reads X, a control is visible, a pane is empty,
   a colour, a layout, what has focus — use your image-viewing tool
   (`view_image` or equivalent) on the capture. This is mandatory and OCR is
   **not** a substitute for it.
   OCR reads text and nothing else. It cannot establish colour, layout,
   geometry, focus, z-order, or — the one that matters most here — **absence**.
   "The pending pane is empty", "no dialog appeared", "the badge is gone" are
   the commonest assertions in these scenarios, and OCR cannot evidence a single
   one: text it does not find is indistinguishable from text it could not read.
   A frame that failed to render is also unreadable, so OCR turns a broken
   capture into a confident wrong verdict in either direction. You may run OCR
   to pull long text out of a frame you have **also** opened; it is triage,
   never the basis of a verdict.
   If you cannot open images at all AND the scenario is `qci:visual:
   required`, the visual assertions are UNOBSERVABLE by you: record
   **ERROR** naming the missing capability. Do not record PASS, do not
   record FAIL, and do not fall back to OCR and grade anyway. If the
   scenario is `qci:visual: none`, missing image capability is not ERROR.
   **NEVER RE-OPEN A PATH; JUDGE DARKNESS ONLY FROM PIXELS YOU JUST OPENED.**
   Your image viewer shows as BLACK any region of an image that repeats, at the
   same position in an image of the same size, something it already showed you
   in this session. So every image the harness writes gets a size of its own (a
   thin black right/bottom margin; the raw screen size is in the frame's `.raw`
   sidecar), and a capture you open for the first time is seen correctly. For
   ANY second look, and for any image the harness did not just hand you (a crop
   you made, a copy), run `vm-gui "$VMNAME" view-copy <image>` (for a crop add
   `--source <capture> --crop WxH+X+Y`) and open the path it prints; never open
   the same file twice. Click-preview `.raw.png` and click-confirm `.post.png`
   files are frames like any other. Decide that a frame is black, blank, or
   missing something ONLY from the pixels of a frame you have just opened —
   never from process state, from rejected attempts, from the harness's
   "same screen pixels" note, or from an earlier frame. Copy a frame together with
   its `.raw` sidecar (`cp F F.raw DEST/`), or use `view-copy`. The harness
   reads your session record afterwards: a PASS or FAIL on a `required`
   scenario whose frames you never opened is recorded ERROR.
7. Before every model-targeted mouse click, activate the window and run
   `vm-gui "$VMNAME" click-preview X Y "visible target label"`. It moves the
   real VM pointer without a button press, then captures the evidence. Read both
   the command-line-generated annotated screenshot and zoomed crop. Confirm the
   cursor aligns with the ring when the renderer captures it; cursor invisibility
   is acceptable. If the red ring is misplaced, generate a corrected preview.
   A preview moves but never clicks. Only after visually confirming the marker may you run
   `vm-gui "$VMNAME" click-confirm <preview-manifest>`. Never use raw
   `vm-gui click X Y` or `xdotool click` for a model-targeted action.
8. Save screenshots, OCR/vision notes, command logs, click preview manifests,
   `click-targets/clicks.tsv`, and journal excerpts under
   the artifact directory. Everything except the harness's own captures is
   triage material, not evidence.
9. Choose exactly one verdict and exit accordingly:
   - **PASS** - every required assertion passed. Exit **0**.
   - **SKIP** - a required dependency is verifiably ABSENT from this
     environment, so the scenario cannot run at all. Write
     `SKIP <one-line reason>` to `status.txt` and exit **0**.
     **A SKIP with a nonzero exit is recorded as a hard failure**, never as a
     skip: the harness accepts `SKIP` only with rc=0, because a skip artifact
     left behind by a process that timed out or was killed is not an
     intentional skip.
   - **FAIL** - an assertion about product behaviour did not hold. Exit
     **nonzero**.
   - **ERROR** - you could not reach a verdict. Exit **nonzero**.

   The SCENARIO verdict is decided by the REQUIRED assertions only. A scenario
   whose required assertions all passed is **PASS** even when one of its own
   OPTIONAL/conditional steps was skipped - a step the scenario itself marks
   "conditional on ...", "skip this step if ...", or "skipped when ...".
   "Some steps skipped, none failed" is PASS, never ERROR. Name the skipped
   step and its reason in the report; do not downgrade the verdict for it.
   ERROR means you could not reach a verdict on the REQUIRED assertions - not
   that the run was less than perfectly complete.

   SKIP is deliberately narrow. It means only: a package, binary, service,
   helper, or image capability that the scenario requires is not installed or
   not available, and you can name it and name the check that showed it absent
   (`command -v foot`, `rpm -q ydotool`, `systemctl status ...`, "the
   `qdistro-fake-lid-close` helper is not in this golden image"). No amount of
   correct driving on your part would make the scenario runnable.

   These are **NOT** skips - record ERROR (nonzero) instead:
   - your own driver/setup commands were malformed, or their state did not
     survive into a later command (see step 11);
   - a required process started and then stopped, or did not respond in time;
   - a command ran but returned output you did not expect;
   - anything you did not manage to observe, where the dependency itself is
     present.
   Calling one of those a SKIP turns a real defect green, which is strictly
   worse than a red row. When in doubt between SKIP and ERROR, choose ERROR.

   Two concrete examples:
   - Good SKIP: `SKIP foot is not installed in this golden image (command -v
     foot -> not found)`, exit 0. The dependency is named, the check is named,
     and no driving would have made the scenario runnable.
   - Bad SKIP (record ERROR instead): "the helper client bound the protocol but
     was gone by the time I ran the steps". Something started and then
     disappeared. That is a defect somewhere -- possibly in your own driving
     (see step 11), possibly in the product -- and calling it a skip makes it
     invisible. Exit nonzero with ERROR.

10. NEVER kill a running `vm-exec` and re-issue the same driver. Its periodic
   `[vm-exec] Waiting... (polls=Ns elapsed=Ns)` lines mean the TRANSPORT IS
   HEALTHY and the guest command is still running - progress, not a wedge.
   vm-exec has its own deadline (`QDISTRO_VM_EXEC_TIMEOUT`, default 1800s) and
   exits 124 when it fires -- but that counter is checked BETWEEN steps, not
   enforced as wall clock, so under host pressure the exit can be far later
   than 1800s. Do NOT simply wait forever for it: put your own wall-clock cap
   around the call, `timeout -k 30 1900 vm-exec ...`, and let THAT be what ends
   it. The `-k` matters: without it a leader that exits on TERM leaves the KILL
   alarm unarmed. Killing it and re-running leaves the
   FIRST driver shell alive in the guest, and two drivers then race on one VM:
   duplicated requests, duplicated rows, no attributable verdict. If a command
   must be abandoned, signal vm-exec (on INT/TERM/HUP it attempts an
   identity-checked TERM-then-KILL of the pinned guest tree, and names any
   descendant it could not pin rather than signalling it) instead of
   SIGKILLing it, and verify in the guest that nothing from the first attempt
   survived before starting a second one -- that verification is load-bearing,
   not a formality, because discovery of a reparented process is not
   guaranteed.
   The guest driver (the ONE root guest shell running Setup through Cleanup;
   other users via `runuser`/`bg_start` inside it) claims the scenario as its
   FIRST commands, in that shell itself:
   `source /tmp/qci-gui-waiters.sh || exit 2` then
   `qci_claim_driver /tmp/qci/<slug>/driver.lock || exit 2`. Exit 2 (library
   missing, lock not openable) is ERROR. The claim lasts until the shell and
   every background process it started exit, so Cleanup stops them. A second
   driver prints `ERROR: a second guest driver is already running` and exits 1.
   Do not delete the lock or change its path; wait for the first driver or
   record ERROR. Short read-only vm-exec checks do not claim.

11. NEVER put a PIPE on vm-exec's stderr. Do NOT open your driver with
   `exec > >(tee "$LOG") 2>&1`, and do not write `out=$(vm-exec ... 2>&1)` or
   `vm-exec ... 2>&1 | reader`. This is the commonest way these drivers hang.
   vm-exec bounds its children's fd 1 internally, but fd 2 is inherited by
   every virsh/jq descendant it starts; the reader then waits for the PIPE to
   reach EOF, which is when the LAST writer closes it, not when vm-exec exits.
   A single descendant outliving vm-exec holds your driver open after the guest
   command has finished, and an outer `timeout` cannot help because the shell
   is blocked on a read rather than on the child. Measured against a vm-exec
   leaving a 4s descendant: `exec > >(tee …) 2>&1` returned after 4.00s, a
   file capture in 0.01s.
   Capture to a regular FILE instead:
       cf=$(mktemp) || exit 2
       exec {w}>"$cf" || { rm -f "$cf"; exit 2; }
       exec {r}<"$cf" || { exec {w}>&-; rm -f "$cf"; exit 2; }
       rm -f "$cf" || { exec {w}>&- {r}<&-; exit 2; }   # check it: an unchecked
                    # unlink leaves the capture NAMED while the command runs
       rc=0
       "$QDISTRO_REPO/scripts/vm/vm-exec" "$VMNAME" 'cmd' \
           >&"$w" 2>&"$w" {w}>&- {r}<&- || rc=$?        # collect rc HERE: with
                    # `set -e` a bare call would abort before you could read it
       exec {w}>&-; out=$(head -c 65536 <&"$r"); exec {r}<&-
       # $rc is vm-exec's status, $out its merged output.
   To log your whole run, redirect to a FILE
   (`exec >"$QCI_GUI_ARTIFACT_DIR/driver.log" 2>&1`):
   a file has no reader to wait on. Use `tee` only where no vm-exec is in scope.
12. Guest logs and scratch must be per-scenario and must not assume a clean
   /tmp. ANY fixed shared guest path (`/tmp/<something>.log`) can already exist
   ROOT-owned from the golden image; a non-root writer then dies with
   `Permission denied` and produces a black screenshot that looks like a
   product failure. Redirect YOUR OWN logs to `/tmp/qci-<slug>/<name>.log`. Do
   not invent a shipped log path to clear: the admin launchers write under
   `${XDG_STATE_HOME:-/home/admin/.local/state}/qdistro/` (admin-app.log,
   qterminal-tui.log), which is per-user and not a shared /tmp path - read it
   for diagnostics, never delete it as root.

13. Run Setup, Steps, Assertions, and Cleanup as ONE guest shell invocation.
   Scenario setup helpers commonly arm an `EXIT` trap that restores the
   compositor's shell role; splitting Setup and Steps across separate
   `vm-exec`/`guest-exec` calls fires that trap the moment Setup's shell exits
   and silently tears down the state your Steps depend on. The resulting
   "precondition missing" is your own teardown, not the environment.

Report format:

```markdown
# <scenario filename> - <PASS|SKIP|FAIL|ERROR>

## Assertions
- [PASS|FAIL|SKIP] <assertion text> - <evidence path and one-line reason>

## Evidence
- <path> - <what it proves>

## Cleanup
- <what was reset>

## Recommendation
<minimal next fix or probe>
```
