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
8. Exit 0 only when every required assertion passes. Exit nonzero on FAIL,
   ERROR, or missing precondition.

Report format:

```markdown
# <scenario filename> - <PASS|FAIL|ERROR>

## Assertions
- [PASS|FAIL] <assertion text> - <evidence path and one-line reason>

## Evidence
- <path> - <what it proves>

## Cleanup
- <what was reset>

## Recommendation
<minimal next fix or probe>
```
