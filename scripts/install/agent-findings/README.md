# agent-findings/

Append-only log of `CHECK` lines produced by `_agent_explore` runs in
`gui-regression-tests.sh`. One file per UTC date; each entry is the
agent name + one CHECK line + any note lines.

Format per entry:

```
<ISO-8601 timestamp>  <agent_name>
    CHECK <name>: <PASS|FAIL|UNEXPECTED> -- <one-sentence detail>
    [optional: UNEXPECTED / note: ... lines]
```

## Why this exists

Each `agent_explore_*` invocation spawns a focused exploration agent
with a constrained Bash sandbox (see `_agent_setup_wrappers`). The
agent's findings used to live in `$TMPDIR/qdistro-gui-tests-$$` and
got nuked on next reboot — discoveries vanished unless someone copy-
pasted from the test output. This directory keeps the discoveries
around long enough to:

1. Audit what an agent has *ever* found, to spot drift between runs.
2. Promote a finding to a tracked bug: copy the CHECK line into a
   `todo/<topic>.md` file with a reproduction, write a deterministic
   regression test in `gui-regression-tests.sh`, and (once the fix
   lands) move the entry to `todo/known-regressions.md`.

## What NOT to put here

- Don't hand-write entries; this is machine-generated.
- Don't commit per-day `.log` files unless they capture a real finding
  worth keeping. The `.gitignore` covers `*.log` here; remove the
  ignore entry locally for a single date if you want to archive it.

## Promoting a finding

1. Read the CHECK line + surrounding `agent.log` (under
   `$TMPDIR/qdistro-gui-tests-<pid>/<agent_name>/agent.log` if the
   suite was run with `--keep-screens`).
2. Write a `todo/<topic>.md` mirroring the structure of the existing
   files there: What / Why we know / Shape of the fix / Test that
   should fail before the fix / Effort.
3. Add a deterministic test to `gui-regression-tests.sh` that fails
   on the unfixed compositor and passes on the fixed one.
4. Land the fix in qdwin / qdshell.
5. Move the entry summary into `todo/known-regressions.md`.
