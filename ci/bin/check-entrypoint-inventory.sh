#!/usr/bin/env bash
# check-entrypoint-inventory.sh — verify every entry point in
# ci/entrypoint-inventory.tsv has at least one mapped test file that exists.
#
# Exits 0 when all test files are present, non-zero when any are missing.
# Designed to be called from gate_selftest in ci/bin/qci.
#
# Usage:
#   ci/bin/check-entrypoint-inventory.sh <qdistro-repo-root>
#
# Output format (one line per finding to stdout):
#   OK   <entry_point> -> <test_file>
#   FAIL <entry_point> -> <test_file>  (file not found)
#
# A summary line is written at the end:
#   ENTRYPOINT-CHECK: N entry-points, M missing test files
#
# The caller (qci gate_selftest) captures this output as a run artifact and
# records it as pass/fail in results.tsv. Intentionally no side-effects:
# no file writes beyond stdout.

set -uo pipefail

REPO="${1:-}"
if [ -z "$REPO" ] || [ ! -d "$REPO" ]; then
    echo "Usage: $0 <qdistro-repo-root>" >&2
    exit 2
fi

INVENTORY="$REPO/ci/entrypoint-inventory.tsv"
if [ ! -f "$INVENTORY" ]; then
    echo "MISSING: $INVENTORY" >&2
    exit 2
fi

total=0
missing=0

while IFS=$'\t' read -r entry_point test_file why_sensitive; do
    # Skip comment lines and the header
    case "$entry_point" in
        '#'*|entry_point|'') continue ;;
    esac

    total=$((total + 1))

    full_test="$REPO/$test_file"
    if [ -f "$full_test" ]; then
        printf 'OK   %-55s -> %s\n' "$entry_point" "$test_file"
    else
        printf 'FAIL %-55s -> %s  (test file not found)\n' "$entry_point" "$test_file"
        missing=$((missing + 1))
    fi
done < "$INVENTORY"

echo ""
echo "ENTRYPOINT-CHECK: $total entry-points checked, $missing missing test file(s)"

if [ "$missing" -gt 0 ]; then
    echo ""
    echo "FAIL: $missing entry point(s) lack a mapped test file."
    echo "Add a test file for each failing entry point, then update ci/entrypoint-inventory.tsv."
    exit 1
fi

echo "PASS: all $total entry-point -> test-file mappings are satisfied."
exit 0
