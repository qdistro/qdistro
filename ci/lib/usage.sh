#!/usr/bin/env bash
# qci module: usage() help text
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

usage() {
    cat <<'EOF'
Usage:
  qci preflight
  qci lint
  qci selftest
  qci host
  qci vm-smoke [--vm <name>]
  qci bats [--vm <name>] [--file <file.bats> ...]
  qci gui [--vm <name>] [--scenario <path.md> ...]
  qci image [--root <extracted-tree>] [--idempotency] [--no-boot]
  qci registry-check
  qci release-manifest
  qci bootstrap-release-profile
  qci affected [--changed-from <ref>] [--run] [--vm <name>] [<path> ...]
  qci edit-guard [--changed-from <ref>] [--allow-test-edits] [<path> ...]
  qci replay <scenario> <vm>
  qci full [--keep-on-fail|--delete-failed-vm]
  qci snapshot-daily [--date YYYY-MM-DD] [--name <vm-name>]
  qci cleanup [--dry-run] [--age-hours N]
  qci report [--latest|--run <run-dir>]
  qci triage [--latest|--run <run-dir>]
  qci list-runs

Notes:
  qci lint    Static pre-VM lint: shellcheck (warn-by-default), bats syntax
              validation, and a heuristic Markdown GUI-scenario structure
              check. Runs without a VM; missing shellcheck/bats => skip+warn.
  qci selftest  Headless self-test of the qci runner itself: runs the host-only
              tests/integration/qci/*.bats suite (no VM, no libvirt) that locks
              down the gate-runner contract — exit-class table, usage/unknown
              dispatch, headless gate manifest+results.tsv, affected/replay/
              offline plumbing. Runs first in the host gate. Missing bats =>
              skip+warn; any failure => EXIT_BATS (35).
  qci image   Static image-content checklist (image/verify-contents.sh) first,
              then the boot/install flow (image/verify.sh, image/install-test.sh).
              Boot/install stages are recorded as blocked when no built image
              is present. --root inspects an extracted tree instead of the
              built artifact; --idempotency runs install twice; --no-boot
              runs only the static checklist.
  qci registry-check  Validate tests/registry.tsv (pilot): each path exists
              and each gate is one qci knows. Advisory only, not enforced.
  qci release-manifest  Assert the source manifest is release-grade (R1): every
              pinned repo checkout is at exactly its commit (+tag) with a CLEAN
              tree, recorded tags agree on one version, and — when a keyring +
              signature are supplied (QDISTRO_RELEASE_KEYRING/_MANIFEST_SIG/
              _RELEASE_SIGNER) — the detached signature verifies and binds to the
              expected signer. READ-ONLY (never checks out). An UNPOPULATED
              manifest or a missing keyring => blocked (dev host has no release
              pins/key yet), NOT failed; a populated-but-divergent manifest
              (wrong commit / dirty tree / moved tag) => EXIT_RELEASE (15).
  qci bootstrap-release-profile  Enforce the hardened/release bootstrap contract
              (R2) HOST-ONLY (no VM): runs the host-static bats that pin no
              `--no-gpg-checks` / no `NOPASSWD: ALL` outside dev and source
              pinned + signature-verified before any root clone/build
              (bootstrap-hardening / source-manifest-signature / gen-source-
              manifest). A `not ok`, a bats crash, or a vacuous 0-test run =>
              EXIT_RELEASE (15); missing bats => blocked.
  qci affected  Map changed file paths to the qci gates that should run, using
              tests/registry.tsv plus conservative path-prefix rules. Paths not
              covered by either map to the FULL gate set (fail-safe: an unknown
              path never narrows coverage). With no paths and --changed-from
              <ref>, derives the changed set from `git diff --name-only <ref>`.
              Prints the selected gates; with --run, runs them (passing --vm to
              the VM gates). Never silently skips: any cap/narrowing is logged.
  qci edit-guard  CI-integrity guard: flag agent edits to PROTECTED paths
              (tests/**, ci/prompts/**, selinux/**) when the current task is
              NOT sanctioned test/CI maintenance. This runs automatically as
              the first step of the host gate (so `qci host` and `qci full`
              enforce it); it can also be invoked standalone. In its CI form
              (no paths, no --changed-from) it derives the changed set from
              `git diff --name-only <base>` PLUS untracked files, where <base>
              is the merge-base of HEAD and the integration branch
              (QCI_BASE_REF). This is deliberately NOT a HEAD diff: agent edits
              are COMMITTED before CI grades them, so a HEAD diff would be empty
              and a committed protected edit would pass clean — the merge-base
              surfaces every commit on the working branch. --changed-from <ref>
              overrides the base verbatim; explicit paths after the args are
              checked verbatim. FAILS (EXIT_USAGE) if any changed path is
              protected, UNLESS sanctioned via --allow-test-edits or
              QCI_ALLOW_TEST_EDITS=1. Fail-safe: an unresolvable integration
              base, a git diff that cannot be computed, or an indeterminate
              (explicit-but-empty) path set FAILS rather than passing-as-clean
              — it never silently hides a protected edit.
  qci replay  Rerun ONE named scenario against an already-preserved (named) VM,
              without provisioning. <scenario> is a bats file (path or basename
              under tests/integration/vm) or a GUI markdown scenario (path or
              basename). The VM must already exist; it is NOT created or
              destroyed (reuses the existing --vm dispatch). qdistro-daily* is
              refused unless QCI_FORCE_PROTECTED_VM=1.

Environment:
  QDWIN_VM_TEMPLATE         Override template domain for disposable test VMs.
  QCI_RUNS_DIR             Artifact directory, default qdistro/ci/runs.
  QCI_AGENT_CMD            Command used for markdown GUI scenarios.
                            It receives the generated prompt file as argv[1],
                            unless the value contains {prompt}.
  QCI_KEEP_FAILED_VM=1      Preserve failed disposable VMs (default). Preserved
                            VMs are hibernated (managedsave) so they stop
                            consuming RAM/CPU; `virsh start <vm>` resumes them
                            for triage.
  QCI_KEEP_FAILED_VM_RUNNING=1  Leave preserved failed VMs RUNNING instead of
                            hibernating them (old behaviour; uses host RAM).
  QCI_DELETE_FAILED_VM=1    Destroy failed disposable VMs after artifacts.
  QCI_FORCE_PROTECTED_VM=1  Permit explicit qdistro-daily* VM targets.
  QCI_ALLOW_TEST_EDITS=1    Sanction edits to protected paths (tests/**,
                            ci/prompts/**, selinux/**) for `qci edit-guard`;
                            same effect as the --allow-test-edits flag. Set
                            this ONLY when the task IS test/CI maintenance.
  QCI_BASE_REF              Integration branch the CI-wired edit-guard diffs
                            against (merge-base with HEAD). Space-separated
                            candidate list; first that resolves wins. Default
                            "origin/main main origin/master master".
  QCI_REQUIRE_AGENT_GUI=1   Make missing QCI_AGENT_CMD fail the gui gate
                            (default for qci gui/full).
  QDWIN_IMG_DIR             libvirt image directory, default
                            ~/.local/share/libvirt/images.
  QCI_OFFLINE=1             Host-only / no-egress posture for VM tests. Records
                            a source tarball + sha256 in manifest.txt, exports
                            the gate down to scenarios so external-network tests
                            self-skip, and (statically clear cases) skips gates
                            whose registry network column is 'external'. Deep
                            podman/container/browser SLIRP-NAT egress audit is a
                            VM task (see todo); this is the env gate + the
                            test-annotation hook.
  QCI_RELEASE=1             Release-profile mode (RC battery): a `blocked` row in
                            a release-relevant gate (vm-smoke / bats / gui /
                            release-manifest / bootstrap-release-profile) is
                            FATAL, not exit-0. "Green" then
                            means zero blocked rows — a missing prerequisite (no
                            built image/VM, an unpopulated/unsigned manifest)
                            cannot pass as success. Escalates an otherwise-passing
                            run to EXIT_RELEASE (15); a real failure keeps its own
                            class. Infra gates and the D1-dropped `image` gate are
                            excluded.
EOF
}
