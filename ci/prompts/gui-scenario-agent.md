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
6. Save screenshots, OCR/vision notes, command logs, and journal excerpts under
   the artifact directory.
7. Exit 0 only when every required assertion passes. Exit nonzero on FAIL,
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
