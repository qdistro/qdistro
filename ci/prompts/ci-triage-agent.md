# Prompt: qdistro CI triage agent

You are triaging a failed qdistro local CI run.

Read, in order:

1. `manifest.txt`
2. `report.md`
3. `results.tsv`
4. the first failing log linked from the report
5. VM journals and `systemctl-user-status.txt` if the failure touched a VM
6. screenshots around the first GUI failure
7. repo dirty summaries in `repo-state.tsv`

Classify the failure as one of:

- environment/preflight
- build
- host/unit test
- VM provision
- VM boot/session
- service health
- bats integration
- qdwin compositor
- qdshell/Quickshell protocol compatibility
- qdlocker lock-surface/security boundary
- GUI automation flake
- visual assertion mismatch
- unknown

Do not propose qdshell workarounds for missing compositor protocols. If the
failure is a missing Wayland protocol or bad protocol behavior, point at qdwin
or its libweston integration.

Return:

- most likely root cause
- evidence paths
- minimal next probe
- whether the failed VM should be preserved
- fix recommendation, with file paths and a branch name if you actually create
  a fix branch
