#!/usr/bin/env bats
#
# Host-only tests for the SHARED TSV notes sanitiser
# (ci/lib/core.sh::tsv_note_sanitize) and its two callers,
# ci/lib/gates/bats.sh::bats_tap_skip_reasons and
# ci/lib/gates/gui.sh::gui_skip_reason.
#
# Why this exists: the GUI skip-reason path received a full control-character
# strip (codex PR1 finding 4); the bats companion-row path, added in the same
# branch, stripped only \t. Both write agent/test-authored text into the same
# results.tsv `notes` column, which ci/lib/report.py reads back with Python
# splitlines() — that breaks on \r \v \f \x1c \x1d \x1e \x85 and U+2028/U+2029
# as well as \n. A bats `skip "..."` reason routinely interpolates command
# output (the compositor-shell reason embeds a VM name), so one stray separator
# split a single companion row into two malformed report rows or truncated its
# `category`. The two paths now share ONE helper so they cannot drift again.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/bats.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    LOG="$BATS_TEST_TMPDIR/suite.bats.log"
    ADIR="$BATS_TEST_TMPDIR/artifacts"
    mkdir -p "$ADIR"
}

# --- the shared helper ------------------------------------------------------

@test "tsv_note_sanitize strips every character Python splitlines() breaks on" {
    local out
    out=$(printf 'a\rb\x0bc\x0cd\x1ce\x1df\x1eg\xc2\x85h\xe2\x80\xa8i\xe2\x80\xa9j' \
        | tsv_note_sanitize)
    [ "$out" = "a b c d e f g h i j" ]
}

@test "tsv_note_sanitize strips tabs and ESC" {
    local out
    out=$(printf 'x\ty\x1b[31mz' | tsv_note_sanitize)
    # \t and ESC are both [:cntrl:] and become spaces; "[31mz" is ordinary text
    # once the ESC that made it an escape sequence is gone.
    [ "$out" = "x y [31mz" ]
}

@test "tsv_note_sanitize flattens markdown noise and collapses whitespace" {
    local out
    out=$(printf '  see `foo` in [the docs](http://x/y)   now  ' | tsv_note_sanitize)
    [ "$out" = "see foo in the docs now" ]
}

@test "tsv_note_sanitize emits no trailing newline" {
    local out
    out=$(printf 'plain\n' | tsv_note_sanitize | od -An -c | tr -s ' ')
    [ "$out" = " p l a i n" ]
}

# --- the bats companion-row path -------------------------------------------

@test "bats_tap_skip_reasons sanitises a reason carrying a bare CR" {
    printf 'ok 1 a case # skip needs foot\ron qci-bats-vm-1\n' > "$LOG"
    local out
    out=$(bats_tap_skip_reasons "$LOG")
    [ "$out" = "needs foot on qci-bats-vm-1" ]
    # One line only: report.read_tsv must not see a second row.
    [ "$(printf '%s' "$out" | wc -l)" -eq 0 ]
}

@test "bats_tap_skip_reasons sanitises U+2028 inside a skip reason" {
    printf 'ok 1 a case # skip policy\xe2\x80\xa8prereq missing\n' > "$LOG"
    [ "$(bats_tap_skip_reasons "$LOG")" = "policy prereq missing" ]
}

@test "bats_tap_skip_reasons still labels a reasonless skip" {
    printf 'ok 1 a case # skip\n' > "$LOG"
    [ "$(bats_tap_skip_reasons "$LOG")" = "(no reason given)" ]
}

@test "bats_tap_skip_reasons dedupes and joins distinct reasons" {
    {
        printf 'ok 1 a # skip alpha\n'
        printf 'ok 2 b # skip alpha\n'
        printf 'ok 3 c # skip beta\n'
    } > "$LOG"
    [ "$(bats_tap_skip_reasons "$LOG")" = "alpha; beta" ]
}

@test "bats_tap_skip_reasons dedupes reasons differing only by a control char" {
    {
        printf 'ok 1 a # skip alpha beta\n'
        printf 'ok 2 b # skip alpha\x0bbeta\n'
    } > "$LOG"
    [ "$(bats_tap_skip_reasons "$LOG")" = "alpha beta" ]
}

@test "bats_tap_skip_reasons caps its output length" {
    printf 'ok 1 a # skip %s\n' "$(head -c 900 < /dev/zero | tr '\0' 'x')" > "$LOG"
    [ "$(bats_tap_skip_reasons "$LOG" | wc -c)" -eq 400 ]
}

# --- the GUI path still behaves identically ---------------------------------

@test "gui_skip_reason routes through the same helper" {
    printf 'SKIP needs `foot`\x0bnot installed\n' > "$ADIR/status.txt"
    [ "$(gui_skip_reason "$ADIR")" = "needs foot not installed" ]
}
