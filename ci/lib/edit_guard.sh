#!/usr/bin/env bash
# qci module: protected-path edit guard
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# qci edit-guard: CI-integrity guard over PROTECTED paths.
#
# An agent tasked with fixing PRODUCT code should not be silently editing the
# tests that grade it, or the prompts/policy that constrain it. This guard
# maps a changed-path set to the protected globs and FAILS unless the edit is
# explicitly sanctioned as test/CI maintenance.
#
# Protected (repo-relative) prefixes:
#   tests/        the test suite (host/bats/gui + registry)
#   ci/prompts/   the agent prompts / anti-cheat policy
#   selinux/      the SELinux confinement policy
#
# Resolution mirrors `qci affected`'s conservative posture: an indeterminate
# input set is a FAIL, never a silent pass — it must never hide a protected
# edit. Indeterminate means: a git working-tree read we cannot compute, or an
# explicit path argument that resolves to nothing. A bare invocation (no ref, no
# paths) is NOT indeterminate: it derives a determinate change set from the
# merge-base with the integration branch (so a COMMITTED protected edit on the
# working branch is caught) plus untracked files. If that base cannot be
# computed it is FAIL-SAFE — a blocked guard, never a silent pass.
# ---------------------------------------------------------------------------
PROTECTED_PREFIXES="tests/ ci/prompts/ selinux/"

# edit_guard_base_ref -> prints the diff base for the CI-wired (no-ref) guard.
#
# This is the crux of the guard's CI value: an agent's edits are COMMITTED on
# the working branch before CI grades them, so diffing against HEAD would see
# an empty tree and a committed protected edit would pass clean. We instead
# diff against the merge-base of HEAD and the integration branch, which yields
# the full set of files this branch touched relative to where it forked from
# the integration line.
#
# Resolution: walk QCI_BASE_REF candidates, pick the first that names a real
# commit, then compute `git merge-base HEAD <cand>`. Prints the resolved base
# SHA on success (exit 0). Prints nothing and exits non-zero when no candidate
# resolves or the merge-base cannot be computed — the caller treats that as a
# fail-safe block, never a pass.
edit_guard_base_ref() {
    local cand base
    for cand in $QCI_BASE_REF; do
        git -C "$QDISTRO_REPO" rev-parse --verify --quiet "$cand^{commit}" >/dev/null 2>&1 || continue
        if base=$(git -C "$QDISTRO_REPO" merge-base HEAD "$cand" 2>/dev/null) && [ -n "$base" ]; then
            printf '%s' "$base"
            return 0
        fi
    done
    return 1
}

# normalize_path <path> -> repo-relative path, for prefix matching.
# Strips a leading repo-root prefix, a leading "./", and any leading "/", so
# that "./tests/x", "/abs/repo/tests/x" and "$QDISTRO_REPO/tests/x" all
# normalize to "tests/x" and cannot slip past the protected-prefix match.
normalize_path() {
    local p=$1 real repo_real
    # An absolute path may reach the repo via a symlinked prefix, so the plain
    # string-strip below would miss it. When the path is absolute and exists,
    # canonicalize it (and the repo root) and strip the canonical repo prefix.
    case "$p" in
        /*)
            if real=$(realpath -m "$p" 2>/dev/null) \
               && repo_real=$(realpath -m "$QDISTRO_REPO" 2>/dev/null); then
                case "$real" in
                    "$repo_real"/*) p=${real#"$repo_real"/} ;;
                esac
            fi
            ;;
    esac
    # Plain (non-canonical) repo-root prefix strip, for the common case.
    case "$p" in
        "$QDISTRO_REPO"/*) p=${p#"$QDISTRO_REPO"/} ;;
    esac
    # Strip a leading "./" (possibly repeated) and any leading "/".
    while [ "${p#./}" != "$p" ]; do p=${p#./}; done
    p=${p#/}
    printf '%s' "$p"
}

# path_is_protected <path> -> 0 (protected) / 1 (not). Normalizes first.
path_is_protected() {
    local p pre
    p=$(normalize_path "$1")
    case "$p" in
        # A bare protected dir (e.g. "tests") with no trailing slash also counts.
        tests|ci/prompts|selinux) return 0 ;;
    esac
    for pre in $PROTECTED_PREFIXES; do
        case "$p" in
            "$pre"*) return 0 ;;
        esac
    done
    return 1
}

gate_edit_guard() {
    qci_assert_run_dir || return $?
    # Args: <changed-from-ref-or-empty> <allow 0/1> <paths...>
    local changed_from=$1 allow=$2; shift 2
    local paths=("$@") log_path="$RDIR/host/edit-guard.log"
    mkdir -p "$(dirname "$log_path")"
    : > "$log_path"

    # The --allow-test-edits flag OR QCI_ALLOW_TEST_EDITS=1 sanctions the edit.
    [ "$QCI_ALLOW_TEST_EDITS" = 1 ] && allow=1

    {
        echo "## edit-guard: protected-path edit check (CI integrity)"
        echo "protected prefixes: $PROTECTED_PREFIXES"
        echo "sanctioned (allow-test-edits): $allow"
        echo
    } >> "$log_path"

    # Derive the changed set. Order of preference:
    #   1. explicit paths on the command line (checked verbatim);
    #   2. else `git diff --name-only <ref>` PLUS untracked, not-ignored files
    #      (`git ls-files --others --exclude-standard`). Untracked files matter:
    #      a *newly created* protected test/prompt is an agent edit the guard
    #      must catch, and a plain diff never lists it.
    #
    # The <ref> is the merge-base with the integration branch (QCI_BASE_REF),
    # NOT HEAD: in the normal agent flow edits are COMMITTED before CI grades
    # them, so a HEAD diff would be empty and a committed protected edit would
    # slip through. The merge-base surfaces every commit on the working branch.
    # An explicit --changed-from <ref> overrides this and is used verbatim.
    #
    # A git read we cannot compute — including an integration base we cannot
    # resolve — is FAIL-SAFE: blocked, not pass.
    local derived_from_git=0
    if [ "${#paths[@]}" -eq 0 ]; then
        local ref diff untracked git_ok=1
        if [ -n "$changed_from" ]; then
            ref=$changed_from
        elif ref=$(edit_guard_base_ref); then
            : # resolved merge-base with the integration branch
        else
            # Fail-safe: the CI-wired guard could not resolve an integration
            # base ref to diff against. Diffing against HEAD here would hide
            # committed protected edits, so block loudly instead.
            {
                echo "FAIL: could not resolve an integration base ref to diff against"
                echo "(candidates tried: $QCI_BASE_REF)"
                echo "(fail-safe: without a base ref a committed protected edit would"
                echo " pass clean, so an unresolvable base is treated as a violation)"
            } >> "$log_path"
            log "edit-guard: no integration base ref resolvable from '$QCI_BASE_REF' — fail-safe block"
            record_result edit-guard base-ref fail "$EXIT_USAGE" args integrity "$log_path" \
                "no integration base ref resolvable (tried: $QCI_BASE_REF); fail-safe"
            cat "$log_path" >&2
            return "$EXIT_USAGE"
        fi
        diff=$(git -C "$QDISTRO_REPO" diff --name-only "$ref" 2>/dev/null) || git_ok=0
        # Untracked files are not relative to <ref>, but a created protected
        # path is still a working-tree edit; always fold them in.
        untracked=$(git -C "$QDISTRO_REPO" ls-files --others --exclude-standard 2>/dev/null) || git_ok=0
        if [ "$git_ok" = 1 ]; then
            while IFS= read -r line; do [ -n "$line" ] && paths+=("$line"); done <<EOF
$diff
$untracked
EOF
            derived_from_git=1
            kv edit_guard_changed_from "$ref"
            {
                echo "changed set derived from: git diff --name-only $ref"
                echo "                          + git ls-files --others --exclude-standard (untracked)"
                echo
            } >> "$log_path"
        else
            # Fail-safe: we were asked to derive the set and could not. Do NOT
            # treat an uncomputable read as "clean" — that would hide a
            # protected edit. Block loudly.
            {
                echo "FAIL: could not read git working-tree state vs '$ref'"
                echo "(fail-safe: an indeterminate change set is treated as a violation,"
                echo " never as pass-as-clean)"
            } >> "$log_path"
            log "edit-guard: git working-tree read against '$ref' failed — fail-safe block"
            record_result edit-guard "$ref" fail "$EXIT_USAGE" args integrity "$log_path" \
                "git diff/ls-files against $ref failed; fail-safe (cannot certify a clean change set)"
            cat "$log_path" >&2
            return "$EXIT_USAGE"
        fi
    else
        {
            echo "changed set: ${#paths[@]} explicit path(s)"
            echo
        } >> "$log_path"
    fi

    # Find the protected paths in the set.
    local p protected=() total=0
    for p in "${paths[@]}"; do
        [ -n "$p" ] || continue
        total=$((total + 1))
        if path_is_protected "$p"; then
            printf 'PROTECTED  %s\n' "$p" >> "$log_path"
            protected+=("$p")
        else
            printf 'ok         %s\n' "$p" >> "$log_path"
        fi
    done

    {
        echo
        echo "$total path(s) checked; ${#protected[@]} touch protected globs"
    } >> "$log_path"

    # An EMPTY derived-from-git set means the working tree genuinely has no
    # changes vs the ref — that is a legitimate clean pass (nothing to guard).
    # An EMPTY *explicit* set, by contrast, is an indeterminate request (caller
    # passed neither a ref nor paths after the args) and is fail-safe blocked.
    if [ "$total" -eq 0 ]; then
        if [ "$derived_from_git" = 1 ]; then
            echo "no changed paths vs ref -> clean (nothing to guard)" >> "$log_path"
            log "edit-guard: no changed paths — clean"
            record_result edit-guard changed-paths pass 0 pass integrity "$log_path" \
                "no changed paths; nothing to guard"
            cat "$log_path" >&2
            return "$EXIT_OK"
        fi
        {
            echo "FAIL: no paths supplied and no --changed-from ref to derive from"
            echo "(fail-safe: an empty explicit input is indeterminate, not 'clean')"
        } >> "$log_path"
        log "edit-guard: empty explicit input with no ref — fail-safe block (indeterminate)"
        record_result edit-guard changed-paths fail "$EXIT_USAGE" args integrity "$log_path" \
            "empty input and no ref to derive from; fail-safe (indeterminate, not clean)"
        cat "$log_path" >&2
        return "$EXIT_USAGE"
    fi

    if [ "${#protected[@]}" -eq 0 ]; then
        log "edit-guard: $total paths, 0 protected — clean"
        record_result edit-guard changed-paths pass 0 pass integrity "$log_path" \
            "$total paths checked; none touch protected globs"
        cat "$log_path" >&2
        return "$EXIT_OK"
    fi

    # Protected paths present.
    if [ "$allow" = 1 ]; then
        {
            echo
            echo "SANCTIONED: ${#protected[@]} protected edit(s) allowed (test/CI maintenance)"
        } >> "$log_path"
        log "edit-guard: ${#protected[@]} protected edits SANCTIONED (allow-test-edits)"
        record_result edit-guard changed-paths pass 0 pass integrity "$log_path" \
            "${#protected[@]} protected edits sanctioned via allow-test-edits"
        cat "$log_path" >&2
        return "$EXIT_OK"
    fi

    {
        echo
        echo "FAIL: ${#protected[@]} protected path(s) edited without --allow-test-edits"
        echo "(set --allow-test-edits or QCI_ALLOW_TEST_EDITS=1 ONLY if this task IS"
        echo " test/CI maintenance)"
    } >> "$log_path"
    log "edit-guard: ${#protected[@]} protected edits, NOT sanctioned — FAIL"
    record_result edit-guard changed-paths fail "$EXIT_USAGE" args integrity "$log_path" \
        "${#protected[@]} protected edits not sanctioned (use --allow-test-edits if intended)"
    cat "$log_path" >&2
    return "$EXIT_USAGE"
}
