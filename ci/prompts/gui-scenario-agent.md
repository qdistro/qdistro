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
5. Capture a screenshot after every GUI action that changes state.
6. Before every model-targeted mouse click, activate the window and run
   `vm-gui "$VMNAME" click-preview X Y "visible target label"`. It moves the
   real VM pointer without a button press, then captures the evidence. Read both
   the command-line-generated annotated screenshot and zoomed crop. Confirm the
   cursor aligns with the ring when the renderer captures it; cursor invisibility
   is acceptable. If the red ring is misplaced, generate a corrected preview.
   A preview moves but never clicks. Only after visually confirming the marker may you run
   `vm-gui "$VMNAME" click-confirm <preview-manifest>`. Never use raw
   `vm-gui click X Y` or `xdotool click` for a model-targeted action.
7. Save screenshots, OCR/vision notes, command logs, click preview manifests,
   `click-targets/clicks.tsv`, and journal excerpts under
   the artifact directory.
8. Choose exactly one verdict and exit accordingly:
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

   SKIP is deliberately narrow. It means only: a package, binary, service,
   helper, or image capability that the scenario requires is not installed or
   not available, and you can name it and name the check that showed it absent
   (`command -v foot`, `rpm -q ydotool`, `systemctl status ...`, "the
   `qdistro-fake-lid-close` helper is not in this golden image"). No amount of
   correct driving on your part would make the scenario runnable.

   These are **NOT** skips - record ERROR (nonzero) instead:
   - your own driver/setup commands were malformed, or their state did not
     survive into a later command (see step 9);
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
     (see step 9), possibly in the product -- and calling it a skip makes it
     invisible. Exit nonzero with ERROR.

9. Run Setup, Steps, Assertions, and Cleanup as ONE guest shell invocation.
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
