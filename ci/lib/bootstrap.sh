#!/usr/bin/env bash
# qci module: constants, env defaults, run state
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

PROJECTS=(
    qdistro
    qdwin
    qdshell
    qdbrowser
    qdchrome-extension
    qdfirefox-extension
    qdgreeter
    qdlocker
    qfileman
    qnotebook
    qterminator
)

# Export a host-side <NAME>_REPO=$WORKSPACE/<name> per project (plus WORKSPACE and
# QDISTRO_REPO). GUI scenarios run in the agent's child shell and source helpers
# via ${QDWIN_REPO}/... / ${QDISTRO_REPO}/...; these vars were process-local, so
# the child inherited none and the anchored paths resolved to empty unless the
# agent guessed. Exporting makes the anchored form cwd-independent. WORKSPACE is
# set by bin/qci before this module is sourced.
if [ -n "${WORKSPACE:-}" ]; then
    export WORKSPACE
    _repo_var=""
    for _repo_proj in "${PROJECTS[@]}"; do
        _repo_var=$(printf '%s' "$_repo_proj" | tr '[:lower:]-' '[:upper:]_')_REPO
        # QDISTRO_REPO is special: bin/qci discovers it from the dispatcher's OWN
        # location ($QCI_DIR/..) and exports it BEFORE sourcing this module. That
        # discovered path is authoritative and is NOT always $WORKSPACE/qdistro —
        # the edit-guard throwaway fixtures (and any copied/renamed checkout) run
        # qci from $BATS_TMP/repo, whose parent holds no `qdistro` dir. Deriving
        # QDISTRO_REPO=$WORKSPACE/qdistro here would clobber the real path and
        # send the git-derived edit-guard checks at a nonexistent tree (which
        # then take the fail-safe "no base ref" path instead of reporting
        # PROTECTED). So: keep an already-discovered QDISTRO_REPO; only derive it
        # from WORKSPACE when it was not set (e.g. bootstrap sourced standalone).
        if [ "$_repo_proj" = qdistro ] && [ -n "${QDISTRO_REPO:-}" ]; then
            export QDISTRO_REPO
            continue
        fi
        export "${_repo_var}=$WORKSPACE/$_repo_proj"
    done
    unset _repo_proj _repo_var
fi

EXIT_OK=0
EXIT_USAGE=2
EXIT_PREFLIGHT=10
EXIT_RELEASE=15
EXIT_BUILD=20
EXIT_HOST=30
EXIT_BATS=35
EXIT_VM_PROVISION=40
EXIT_VM_BOOT=50
EXIT_SERVICE=60
EXIT_GUI=70
EXIT_VISUAL=80
EXIT_RUNNER=90

RDIR=""
GATE=""
EXPLICIT_VM=""
KEEP_FAILED_DEFAULT=1
CREATED_VMS=()
# Per-run "golden" backing disks: the compositor is built ONCE per run into a
# golden qcow2, then every disposable worker VM clones from it (skipping the
# expensive in-guest fresh-vm-bootstrap build). These hold the golden DISK paths
# (not domains — the golden domain is undefined after a clean shutdown so its
# disk is a stable read-only backing). Tracked separately from CREATED_VMS so
# release_vm/worker cleanup never touches them.
RUN_GOLDEN_BATS=""
RUN_GOLDEN_GUI_ADMIN=""
RUN_GOLDEN_GUI_QDWIN=""
RUN_GOLDEN_DISKS=()
GOLDEN_INFLIGHT_VMS=()   # golden VMs mid-build (for interrupt cleanup)
GOLDEN_PRESERVE=0        # set if a preserved failed worker may back-reference a golden
FINALIZING=0

# QCI_OFFLINE=1 forces a host-only / no-egress posture for VM tests:
#   - tests/scenarios see QCI_OFFLINE=1 in their environment and are expected
#     to self-skip anything that needs external network (the annotation hook);
#   - the runner records the source tree as a tarball + sha256 in manifest.txt
#     so a run is reproducible without re-fetching anything;
#   - gates whose registry `network` column is 'external' are skipped where
#     that is statically knowable.
# The deep podman/container/browser egress audit under SLIRP NAT is a separate
# VM task; this is the env gate + plumbing only.
QCI_OFFLINE="${QCI_OFFLINE:-0}"
[ "$QCI_OFFLINE" = 1 ] && export QCI_OFFLINE

# QCI_RELEASE=1 is the release-profile mode (05-agent-test-plan.md §A): "green"
# for the RC battery means ZERO blocked rows, not just zero failed rows. A
# `blocked` row in a release-relevant gate (a missing prerequisite — no built
# image, no test VM, an unpopulated/unsigned manifest) is recorded `blocked`
# and exits 0 in normal mode; under QCI_RELEASE=1 it escalates the run to
# EXIT_RELEASE so a missing prerequisite cannot pass as success at RC. Scoped to
# the gates whose blocked rows mean "the release was not actually exercised";
# infra gates (preflight/selftest/lint/registry-check/affected/edit-guard) and
# the D1-dropped `image` gate are intentionally excluded.
QCI_RELEASE="${QCI_RELEASE:-0}"
[ "$QCI_RELEASE" = 1 ] && export QCI_RELEASE
QCI_RELEASE_FATAL_GATES="vm-smoke bats gui release-manifest bootstrap-release-profile"

# QCI_ALLOW_TEST_EDITS=1 sanctions edits to the `qci edit-guard` protected
# paths (tests/**, ci/prompts/**, selinux/**). Same effect as the
# --allow-test-edits flag. Default 0: a protected edit FAILS the guard.
QCI_ALLOW_TEST_EDITS="${QCI_ALLOW_TEST_EDITS:-0}"

# QCI_BASE_REF names the integration branch the edit-guard diffs against when
# it runs as part of CI (i.e. with no explicit --changed-from / paths). The
# CI-wired guard does NOT diff against HEAD — by the time CI grades a run the
# agent's edits are already COMMITTED, so a HEAD diff would be empty and a
# committed protected edit would pass clean. Instead it diffs against the
# merge-base of HEAD and this integration ref, which surfaces every commit on
# the working branch. Space-separated candidate list; the first that resolves
# wins. Default tries the local/remote main branch.
QCI_BASE_REF="${QCI_BASE_REF:-origin/main main origin/master master}"
