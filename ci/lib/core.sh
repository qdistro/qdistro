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
