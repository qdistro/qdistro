#!/usr/bin/env bash
# qci module: lint gate (shellcheck/bats-syntax/md)
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# Static pre-VM lint (additive, host-only, degrades gracefully).
#
# Checks are warn-only unless they protect an executable contract:
#   1. shellcheck over a bounded set of first-party shell scripts.
#   2. bats syntax validation (bats --count) over the VM bats files.
#   3. heuristic Markdown structure check over GUI scenarios.
#   4. local-link and heading-anchor validation over maintained documentation.
#   5. GUI scenario flake-smell lint (strict only with QCI_FLAKE_STRICT=1).
# Missing shellcheck/bats => recorded as skip with a clear message.
# Shellcheck and scenario structure remain migration metrics. Bats parse errors,
# broken documentation links, and strict flake findings return a nonzero class.
# preflight deliberately ignores that return; standalone `qci lint` and affected
# runs receive it.
# ---------------------------------------------------------------------------
lint_shell_files() {
    # Bounded, first-party set; excludes vendored/build trees.
    find "$QDISTRO_REPO/scripts" "$QDISTRO_REPO/ci/bin" "$QDISTRO_REPO/ci/lib" "$QDISTRO_REPO/image" \
        -type f -name '*.sh' 2>/dev/null
    find "$QDISTRO_REPO/ci/bin" -type f ! -name '*.*' 2>/dev/null   # qci itself
    find "$QDISTRO_REPO/tests/integration" -maxdepth 2 -type f -name '*.sh' 2>/dev/null
}

gate_lint() {
    qci_assert_run_dir || return $?
    local log_path="$RDIR/host/lint.log"
    local rc=$EXIT_OK step_rc
    mkdir -p "$(dirname "$log_path")"
    : > "$log_path"

    # 1. shellcheck (warn-by-default; tree not confirmed clean).
    if command -v shellcheck >/dev/null 2>&1; then
        local files=() f findings=0 file_errs
        while IFS= read -r f; do [ -n "$f" ] && files+=("$f"); done < <(lint_shell_files | sort -u)
        {
            echo "## shellcheck (${#files[@]} files)"
            echo "shellcheck $(shellcheck --version 2>/dev/null | awk '/version:/{print $2}')"
            echo
        } >> "$log_path"
        # Count findings per file but never hard-fail the gate.
        for f in "${files[@]}"; do
            file_errs=$(shellcheck -f gcc "$f" 2>/dev/null | grep -c ':' || true)
            if [ "${file_errs:-0}" -gt 0 ]; then
                printf '%s: %s findings\n' "$f" "$file_errs" >> "$log_path"
                findings=$((findings + file_errs))
            fi
        done
        printf '\nTOTAL shellcheck findings: %s across %s files\n' "$findings" "${#files[@]}" >> "$log_path"
        log "lint: shellcheck $findings findings across ${#files[@]} files"
        if [ "$findings" -eq 0 ]; then
            record_result lint shellcheck pass 0 pass lint "$log_path" "${#files[@]} files clean"
        else
            record_result lint shellcheck skip 0 pass lint "$log_path" "$findings findings (warn-only, not enforced)"
        fi
    else
        record_skip lint shellcheck lint "shellcheck not installed; skipping shell static lint"
        log "lint: shellcheck not installed (skip)"
    fi

    # 2. bats syntax validation (no VM): bats --count parses each file.
    if command -v bats >/dev/null 2>&1; then
        local bf bats_files=() bad=0 nbf
        while IFS= read -r bf; do [ -n "$bf" ] && bats_files+=("$bf"); done \
            < <(find "$QDISTRO_REPO/tests/integration/vm" -maxdepth 1 -name '*.bats' -type f | sort)
        { echo; echo "## bats syntax (bats --count over ${#bats_files[@]} files)"; } >> "$log_path"
        # helpers.bash hard-requires VM_NAME at `load` time; supply a dummy so
        # bats --count exercises real parsing instead of failing on the guard.
        # We never boot anything here — this is syntax validation only.
        for bf in "${bats_files[@]}"; do
            if nbf=$(VM_NAME="${VM_NAME:-qci-lint-syntax}" bats --count "$bf" 2>>"$log_path"); then
                printf 'OK   %s: %s tests\n' "$(rel_path "$bf")" "$nbf" >> "$log_path"
            else
                printf 'PARSE-ERROR %s\n' "$(rel_path "$bf")" >> "$log_path"
                bad=$((bad + 1))
            fi
        done
        log "lint: bats syntax $bad parse errors across ${#bats_files[@]} files"
        if [ "$bad" -eq 0 ]; then
            record_result lint bats-syntax pass 0 pass lint "$log_path" "${#bats_files[@]} bats files parse cleanly"
        else
            record_result lint bats-syntax fail "$EXIT_BATS" bats lint "$log_path" "$bad bats files failed to parse"
            rc=$EXIT_BATS
        fi
    else
        record_skip lint bats-syntax lint "bats not installed; skipping bats syntax validation"
        log "lint: bats not installed (skip)"
    fi

    # 3. heuristic Markdown GUI-scenario structure check (non-fatal).
    local md mds=() md_missing=0 nmd=0
    while IFS= read -r md; do [ -n "$md" ] && mds+=("$md"); done < <(
        find "$QDISTRO_REPO/tests/integration" -type f -name '[0-9][0-9]-*.md' 2>/dev/null | sort
    )
    { echo; echo "## markdown GUI-scenario structure (${#mds[@]} scenarios)"; } >> "$log_path"
    for md in "${mds[@]}"; do
        nmd=$((nmd + 1))
        # Heuristic: expect a top-level title plus Setup and Steps sections.
        if grep -qE '^#[[:space:]]' "$md" \
            && grep -qiE '^##[[:space:]]+setup' "$md" \
            && grep -qiE '^##[[:space:]]+steps' "$md"; then
            : # well-formed
        else
            printf 'WARN  %s: missing one of title/##Setup/##Steps\n' "$(rel_path "$md")" >> "$log_path"
            md_missing=$((md_missing + 1))
        fi
    done
    printf '\n%s/%s scenarios well-formed; %s missing expected headings\n' \
        "$((nmd - md_missing))" "$nmd" "$md_missing" >> "$log_path"
    log "lint: markdown $md_missing/$nmd scenarios missing expected headings"
    if [ "$md_missing" -eq 0 ]; then
        record_result lint md-structure pass 0 pass lint "$log_path" "$nmd scenarios well-formed"
    else
        record_result lint md-structure skip 0 pass lint "$log_path" "$md_missing/$nmd scenarios missing headings (heuristic, non-fatal)"
    fi

    # 4. Maintained documentation must not point at missing local files or
    # headings. External URLs remain outside this deterministic host-only check.
    local doc_lint="$QCI_BIN_DIR/doc-link-lint.py"
    { echo; echo "## documentation local links"; } >> "$log_path"
    if [ ! -f "$doc_lint" ] || ! command -v python3 >/dev/null 2>&1; then
        record_result lint doc-links fail "$EXIT_HOST" host lint "$log_path" \
            "doc-link-lint.py or python3 absent; cannot validate maintained documentation"
        [ "$rc" -eq 0 ] && rc=$EXIT_HOST
    elif python3 "$doc_lint" --root "$QDISTRO_REPO" >> "$log_path" 2>&1; then
        record_result lint doc-links pass 0 pass lint "$log_path" \
            "README, doc/, and ci/*.md local links valid"
    else
        record_result lint doc-links fail "$EXIT_HOST" host lint "$log_path" \
            "broken local documentation link or heading anchor"
        [ "$rc" -eq 0 ] && rc=$EXIT_HOST
    fi

    # 5. flake-smell lint over GUI scenarios. Flags the recurring flake sources
    # (fixed sleeps before assertions, unscoped journal reads, one-shot
    # systemctl/virsh, relative source paths, self-matching pgrep, unscoped /tmp
    # writes, screenshot-only asserts, backgrounded waits, prose-only assertions)
    # so they can be migrated to the waiter library / scenario-tmpdir contract.
    # The `fl_findings` count (non-allowlisted; ci/scenario-flake-allow.tsv waives
    # documented cases) is the migration-progress metric. WARN-ONLY by default;
    # set QCI_FLAKE_STRICT=1 to hard-fail the gate on any non-allowlisted finding
    # (the Phase-1 exit criterion, adopted once the count is driven to the
    # allowlist).
    lint_flake_smells "$log_path"; step_rc=$?
    [ "$rc" -eq 0 ] && [ "$step_rc" -ne 0 ] && rc=$step_rc

    return "$rc"
}

# TWO separate strict flake-smell metrics, recorded as SEPARATE rows so the report
# can never conflate "qdistro-owned is strict-clean" with "umbrella is
# strict-clean" (the plan's §3 requirement):
#   qdistro-owned strict = qdistro/tests/integration/{permissions-gui,
#                          qdwin-noctalia} — the migration metric for the scenarios
#                          this repo owns and can fix directly.
#   umbrella      strict = the SAME path set `qci gui` schedules (qdistro + sibling
#                          qdwin/qdlocker roots) — the readiness metric for widening
#                          `qci gui` across the whole suite.
# Warn-only stays the default for BOTH; QCI_FLAKE_STRICT=1 escalates each to a fail
# row exactly as before. Factored out of gate_lint so the two-row contract is
# host-testable. Arg: log_path.
lint_flake_smells() {
    local log_path=$1
    local fl_script="$QCI_BIN_DIR/scenario-flake-lint.py" fl_findings fl_set fl_subject
    local rc=$EXIT_OK
    if [ ! -f "$fl_script" ] || ! command -v python3 >/dev/null 2>&1; then
        record_skip lint flake-smells lint "scenario-flake-lint.py or python3 absent; skipping flake-smell lint"
        return 0
    fi
    local fl_strict=${QCI_FLAKE_STRICT:-0}
    { echo; echo "## scenario flake-smell lint (strict=$fl_strict)"; } >> "$log_path"
    for fl_set in qdistro umbrella; do
        fl_subject="flake-smells-$fl_set"
        { echo; echo "### set=$fl_set"; } >> "$log_path"
        python3 "$fl_script" --set "$fl_set" --format gcc >> "$log_path" 2>>"$log_path"
        fl_findings=$(python3 "$fl_script" --set "$fl_set" --format summary 2>&1 >/dev/null \
            | awk '/scenario-flake-lint:/{print $2; exit}')
        [ -n "$fl_findings" ] 2>/dev/null || fl_findings=0
        if [ "$fl_findings" -eq 0 ] 2>/dev/null; then
            log "lint: scenario flake-smells[$fl_set] 0 finding(s)"
            record_result lint "$fl_subject" pass 0 pass lint "$log_path" \
                "no flake-smell findings ($fl_set set)"
        elif [ "$fl_strict" = 1 ]; then
            log "lint: scenario flake-smells[$fl_set] $fl_findings finding(s) (STRICT — failing)"
            record_result lint "$fl_subject" fail "$EXIT_HOST" lint lint "$log_path" \
                "$fl_findings non-allowlisted flake-smell finding(s) in $fl_set set (QCI_FLAKE_STRICT=1)"
            rc=$EXIT_HOST
        else
            log "lint: scenario flake-smells[$fl_set] $fl_findings finding(s) (warn-only)"
            record_result lint "$fl_subject" skip 0 pass lint "$log_path" \
                "$fl_findings flake-smell finding(s) in $fl_set set (warn-only; migrate to waiter lib / scenario-tmpdir)"
        fi
    done
    return "$rc"
}
