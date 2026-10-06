# b19 — fable-remediation acceptance run + preserved-VM replays

Run dir: `bats-20261004T123605Z-3058184` (disposable tree
`/var/tmp/t3s-qci-b-4729fce58`, pinned `920351f70`).
Result: **6/10 files pass** — s123, s124, s127, s129 failed on driver +
product defects that the replays below closed.

## b19 failures and root causes

| File | b19 failure | Root cause |
|---|---|---|
| s123 | `link.sock` mode/topology asserts | waypipe `-o` unlinks `link.sock` at accept — post-attach evidence impossible; channel lives in the gofer netns, not the host socket table. Also: runuser's PAM session resets umask → the socket actually bound `0755`, not the claimed `0600` (spawn fix `7133aee42`). |
| s124 | `io.podman.annotations.seccomp` empty; `awk` missing | podman inlines the parsed profile — no annotation exists (evidence moved to `.Config.CreateCommand` argv); container image is coreutils-only (`grep`+`cut`). |
| s127 | bound allow, focus-clear, forge asserts | (a) qdshell's async `busctl VerifyClientIdentity` was AccessDenied — method missing from `_QDSHELL_GATE_METHODS` (broker fix `36d779097`); (b) `$!` registered the runuser wrapper pid, not secctx-exec's fork child (launch-record registration); (c) stale journal matches across preserved runs (cursor scoping); (d) a stale `CLIPBOARD_GATE src_silo=s127b` let `injectFocus` race ahead of B's recorded offer (per-step cursor `J5`); (e) qdlocker idle-locked the session mid-run (widen+restore). |
| s129 | fd-attack attribution | `pgrep -f $CTR` matched conmon/podman sockets — unattributable; the channel fd lives in the gofer's netns (`nsenter -t <gofer> -n ss -xp` peer-map). |

## replays/ — preserved-VM verification (post-remediation)

Each driver rerun on its preserved b19 worker with the product fixes
hand-deployed (broker .py, spawn-tier3s.sh, helper binary) — the same
content b20 bakes:

| Log | Result | Notes |
|---|---|---|
| `s129-replay.log` | 68/10 | attack asserts all pass; fails are only leftover `s129x` manual-investigation silo cleanliness |
| `s129-replay2.log` | **78/0** | after removing the stale silo |
| `s123-r2.log` | 75/1 | only `link.sock` mode — `755:1000` observed, proving the runuser/PAM umask reset |
| `s123-r3.log` | 73/3 | count greps hit prior-run journal lines (fresh VM has no such problem) |
| `s123-r4.log` | **76/0** | spawn umask fix + cursor-scoped count greps |
| `s124-r2.log` | 74/1 | seccomp argv + grep/cut fixes pass; only `mapped handle=1` count=2 (stale journal) |
| `s124-r3.log` | **75/0** | cursor-scoped greps |
| `s127-r4.log` | 93/1 | VerifyClientIdentity authorized; bound same-silo `verdict=allow` + `identity.verify` audit rows; only locker-restore sentinel |
| `s127-r5.log` | **94/0** | ABSENT sentinel for absent->absent locker.conf |

## commits carrying the remediation

- `36d779097` broker: admit VerifyClientIdentity on the qdshell-gate exe path
- `7133aee42` tier3s: deliver the promised 0600 on link.sock
- `b6523e801` qdshell: bind the v23 sidecar to the focused handle's attested tag
- `45f7bda59` tests: tier3s VM drivers — preserved-replay hardening + bound-allow evidence
- `e96442f4a` tests: s124 — seccomp argv evidence + coreutils-only in-container probes

b20 (`qci-tier3s-b20b`, `e96442f4a`) re-runs the full 10-file lane on
fresh workers as the record attempt.
