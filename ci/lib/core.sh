#!/usr/bin/env bash
# qci module: primitives (log/stamp/exit-class/rel_path/kv)
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

now_utc() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }
stamp() { date -u +'%Y%m%dT%H%M%SZ'; }
log() { printf '[qci] %s\n' "$*" >&2; }

safe_name() {
    printf '%s' "$1" | tr '/: @' '____' | tr -cd 'A-Za-z0-9._-'
}

exit_class_name() {
    case "$1" in
        0) echo pass ;;
        10) echo preflight ;;
        15) echo release ;;
        20) echo build ;;
        30) echo host ;;
        35) echo bats ;;
        40) echo vm_provision ;;
        50) echo vm_boot ;;
        60) echo service ;;
        70) echo gui ;;
        80) echo visual ;;
        90) echo runner ;;
        *) echo "unknown($1)" ;;
    esac
}

# Map a child process exit code to a qci exit class. KNOWN LIMITATION: qci's
# class codes share the integer space with raw child codes, so a sub-tool that
# happens to exit with one of these values (e.g. a script that `exit 40`s) is
# passed through and recorded under THAT class (vm_provision) instead of the
# caller's $default. Real test runners use small codes (pytest 1-5, npm 1), so
# this rarely bites; a future cleanup could namespace qci's own rc separately.
map_rc() {
    local rc=$1 default=$2
    case "$rc" in
        0|10|15|20|30|35|40|50|60|70|80|90) echo "$rc" ;;
        *) echo "$default" ;;
    esac
}

rel_path() {
    local path=${1:-}
    [ -n "$path" ] || return 0
    case "$path" in
        "$RDIR"/*) printf '%s' "${path#$RDIR/}" ;;
        *) printf '%s' "$path" ;;
    esac
}

kv() {
    printf '%s=%s\n' "$1" "$2" >> "$RDIR/manifest.txt"
}

# Sanitise one agent/test-authored string for a TSV notes column.
#
# The notes column is read back by report.py with Python `splitlines()`, which
# breaks on far more than \n: \r \v \f \x1c \x1d \x1e \x85 and the Unicode
# separators U+2028/U+2029. A reason string that interpolates command output (a
# bats `skip "..."` message, or an agent-written status.txt) can therefore split
# ONE result row into two malformed report rows, or truncate its `category`; an
# ESC would inject a terminal escape sequence into the rendered report.
#
# Both the GUI skip-reason path (gui_skip_reason) and the bats companion-row path
# (bats_tap_skip_reasons) feed such text into the same column, so they share this
# helper rather than each carrying a partial strip. Markdown noise (inline code
# backticks, `[text](link)`) is flattened first because these strings are
# frequently lifted out of a markdown report. Whitespace is collapsed and
# trimmed; nothing is length-capped here (each caller owns its own cap).
#
# Control characters are REPLACED WITH A SPACE, not deleted: deleting them
# silently welds two words together ("policy\x0bprereq" -> "policyprereq"),
# which is both unreadable and a dedupe hazard for the bats path, where two
# reasons differing only by a stray separator must collapse to one. The
# collapse-and-trim pass afterwards means a replaced character costs nothing
# when it sat next to existing whitespace.
#
# Reads the string from stdin, writes the sanitised form to stdout with no
# trailing newline (the final `tr -d` covers the case where the caller does not
# use command substitution). Pure text transform => host-testable.
tsv_note_sanitize() {
    sed -E 's/\[([^]]*)\]\([^)]*\)/\1/g; s/`//g' \
        | tr '[:cntrl:]' ' ' \
        | sed -E 's/\xc2\x85|\xe2\x80\xa8|\xe2\x80\xa9/ /g; s/[[:space:]]+/ /g; s/^ //; s/ $//' \
        | tr -d '\n'
}
