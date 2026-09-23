#!/usr/bin/env bash
# qdshell local CI runner — equivalent to what GitHub Actions / a
# self-hosted runner would invoke. Five gates:
#   1. qmltest      — Tests/tst_*.qml under Tests/
#   2. qmllint      — informational (counts Warning/Error rows)
#   3. qmlformat    — --files-changed dry-run check
#   4. integration  — bats scenarios on a broker-present VM (skipped if
#                     qdistro repo not adjacent or VM not running)
#   5. summary      — pass/fail tally + non-zero exit on hard fail
#
# Invocation:
#   ./scripts/ci-local.sh             # all gates, fail on qmltest only
#   ./scripts/ci-local.sh --strict    # also fail on lint warnings
#   ./scripts/ci-local.sh --no-int    # skip integration gate
#   ./scripts/ci-local.sh --quick     # qmltest only

set -euo pipefail

cd "$(dirname "$0")/.."

# --- options ---------------------------------------------------------

STRICT=0
NO_INT=0
QUICK=0
for arg in "$@"; do
    case "$arg" in
        --strict)  STRICT=1 ;;
        --no-int)  NO_INT=1 ;;
        --quick)   QUICK=1 ;;
        -h|--help)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# --- locate Qt6 binaries --------------------------------------------

find_qt_tool() {
    local tool=$1
    local candidate
    # Prefer explicit Qt6 paths over command -v, which may find a
    # qtchooser wrapper that dispatches to a missing Qt5 install.
    for candidate in \
        "/usr/lib64/qt6/bin/$tool" \
        "/usr/lib/qt6/bin/$tool" \
        "/usr/lib64/qt6/libexec/$tool" \
        "/usr/lib/qt6/libexec/$tool" \
        "$(command -v "$tool" 2>/dev/null || true)"
    do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    printf '%s\n' "$tool"
}

QMLTEST="${QMLTEST:-$(find_qt_tool qmltestrunner)}"
QMLLINT="${QMLLINT:-$(find_qt_tool qmllint)}"
QMLFORMAT="${QMLFORMAT:-$(find_qt_tool qmlformat)}"

for tool in "$QMLTEST" "$QMLLINT" "$QMLFORMAT"; do
    if [ ! -x "$tool" ]; then
        echo "missing tool: $tool" >&2
        echo "  install Qt6 dev tools (qt6-declarative-tools / similar)" >&2
        exit 2
    fi
done

# --- color/log helpers ----------------------------------------------

if [ -t 1 ]; then
    RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'
    BLUE=$'\e[34m'; BOLD=$'\e[1m'; RESET=$'\e[0m'
else
    RED=; GREEN=; YELLOW=; BLUE=; BOLD=; RESET=
fi

step() { printf '%s==> %s%s\n' "$BLUE$BOLD" "$1" "$RESET"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$RESET"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$RESET"; }
err()  { printf '%s%s%s\n' "$RED" "$1" "$RESET"; }

# --- 1. qmltest -----------------------------------------------------

QMLTEST_PASS=0
QMLTEST_FAIL=0
QMLTEST_FILES=0

step "qmltest"
for f in Tests/tst_*.qml; do
    if [ ! -f "$f" ]; then continue; fi
    QMLTEST_FILES=$((QMLTEST_FILES + 1))
    out="$("$QMLTEST" -input "$f" 2>&1 || true)"
    line="$(printf '%s\n' "$out" | grep -E '^Totals:' | tail -1 || true)"
    if [ -z "$line" ]; then
        err "  $f: NO TOTALS LINE — runner failed"
        QMLTEST_FAIL=$((QMLTEST_FAIL + 1))
        continue
    fi
    pass="$(echo "$line" | sed -nE 's/.*Totals: ([0-9]+) passed.*/\1/p')"
    fail="$(echo "$line" | sed -nE 's/.* ([0-9]+) failed.*/\1/p')"
    pass="${pass:-0}"; fail="${fail:-0}"
    QMLTEST_PASS=$((QMLTEST_PASS + pass))
    QMLTEST_FAIL=$((QMLTEST_FAIL + fail))
    if [ "$fail" -gt 0 ]; then
        err "  $f: $pass passed, $fail failed"
        # Re-run with -v2 to dump per-test results.
        "$QMLTEST" -input "$f" 2>&1 | grep -E '^FAIL' | sed 's/^/    /' || true
    else
        ok  "  $f: $pass passed"
    fi
done
echo "  $QMLTEST_FILES file(s); $QMLTEST_PASS passed, $QMLTEST_FAIL failed"

if [ "$QUICK" = 1 ]; then
    if [ "$QMLTEST_FAIL" -gt 0 ]; then exit 1; fi
    exit 0
fi

# --- 1b. jstest (node unit tests) ----------------------------------
#
# Pure-logic modules (Services/**/*.js) are unit-tested with plain Node
# scripts under tests/test_*.js (CommonJS; see tests/test_clipboard_silo.js).
# They are also declared as meson test() targets, but qci's qdshell host
# step runs this script rather than `meson test`, so run them here too so
# both qci and local `ci-local.sh` cover them. Node-less hosts skip.

JSTEST_PASS=0
JSTEST_FAIL=0
JSTEST_FILES=0

step "jstest (node)"
NODE_BIN="${NODE:-$(command -v node || true)}"
if [ -z "$NODE_BIN" ]; then
    warn "  node not found — skipping JS unit tests"
else
    for f in tests/test_*.js; do
        if [ ! -f "$f" ]; then continue; fi
        JSTEST_FILES=$((JSTEST_FILES + 1))
        if out="$("$NODE_BIN" "$f" 2>&1)"; then
            ok  "  $f: ok"
            JSTEST_PASS=$((JSTEST_PASS + 1))
        else
            err "  $f: FAIL"
            printf '%s\n' "$out" | tail -20 | sed 's/^/    /'
            JSTEST_FAIL=$((JSTEST_FAIL + 1))
        fi
    done
    echo "  $JSTEST_FILES file(s); $JSTEST_PASS passed, $JSTEST_FAIL failed"
fi

# --- 2. qmllint -----------------------------------------------------

step "qmllint"

# Run lint over the entire QML tree. Category levels are configured
# via the repo-root .qmllint.ini; cascade-from-missing-Quickshell
# categories (UnqualifiedAccess, MissingProperty, UnresolvedType,
# RequiredProperty, ImportFailure, UnresolvedAlias) are disabled there
# because Quickshell is not installed on the host and every consumer
# site would otherwise cascade into thousands of unactionable
# warnings.
#
# qmllint 6.11 has no --strict flag; --strict on the CLI was a
# Noctalia-era assumption that didn't survive a Tumbleweed Qt6.11
# upgrade. The .qmllint.ini approach replaces it.
LINT_OUT="$(find Modules Services Widgets Commons Helpers Tests \
    -name '*.qml' -print0 \
    | xargs -0 "$QMLLINT" 2>&1 || true)"

# Filter out import-resolution noise (Quickshell / qs.* not present
# on host) — those are a fixture of running lint without the runtime.
LINT_REAL="$(printf '%s\n' "$LINT_OUT" | \
    grep -E '^(Warning|Error)' | \
    grep -vE 'Failed to import|Warnings occurred while importing module|Could not link' \
    || true)"

LINT_WARN_COUNT="$(printf '%s\n' "$LINT_REAL" | grep -c '^Warning' || true)"
LINT_ERR_COUNT="$( printf '%s\n' "$LINT_REAL" | grep -c '^Error'   || true)"

# grep -c with no matches and 'set -o pipefail' / && / || dance can
# leave these as the literal string "0\n" — strip whitespace.
LINT_WARN_COUNT="${LINT_WARN_COUNT//[!0-9]/}"
LINT_ERR_COUNT="${LINT_ERR_COUNT//[!0-9]/}"
LINT_WARN_COUNT="${LINT_WARN_COUNT:-0}"
LINT_ERR_COUNT="${LINT_ERR_COUNT:-0}"

# In --strict mode, dump the first 40 warnings to make the gate
# actionable. Without this the user has to re-run by hand.
if [ "$STRICT" = 1 ] && [ "$LINT_WARN_COUNT" -gt 0 ]; then
    printf '%s\n' "$LINT_REAL" | head -40 | sed 's/^/    /'
    if [ "$LINT_WARN_COUNT" -gt 40 ]; then
        printf '    ... %d more\n' "$((LINT_WARN_COUNT - 40))"
    fi
fi

echo "  $LINT_WARN_COUNT warning(s), $LINT_ERR_COUNT error(s) (excluding import noise)"

# --- 3. qmlformat --check ------------------------------------------

step "qmlformat --check (Services/Qdshell/ only)"

# qmlformat-checking the entire fork-base tree triggers diffs against
# upstream Noctalia's formatting choices that aren't ours to police.
# Limit to NEW code we've authored: Services/Qdshell/ singletons.
#
# Two intentional exclusions:
#   * Tests/tst_*.qml — qmlformat would explode the intentional
#     one-liner test bodies (each `function test_x() { compare(a,b) }`
#     gets blown up to 3 lines). Tests are formatted by their own
#     readability convention.
#   * Services/Qdshell/{HooksGate,Lock,Notifications}.qml — these are
#     Phase-5 broker integration singletons that are required to stay
#     byte-stable under the audit trail. Reformatting them is a
#     deliberate, separately-tracked change.

FMT_FAIL=0
FMT_EXCLUDE_RE='Services/Qdshell/(HooksGate|Lock|Notifications)\.qml$'
for f in Services/Qdshell/*.qml; do
    if [ ! -f "$f" ]; then continue; fi
    if printf '%s\n' "$f" | grep -qE "$FMT_EXCLUDE_RE"; then
        echo "  $f: skipped (byte-stable broker integration)"
        continue
    fi
    if ! "$QMLFORMAT" --version >/dev/null 2>&1; then
        warn "  $f: qmlformat unavailable, skipping"
        continue
    fi
    # qmlformat 6.11 doesn't have --check; substitute a diff-against-
    # in-place format.
    if ! diff -q "$f" <("$QMLFORMAT" "$f" 2>/dev/null) >/dev/null 2>&1; then
        warn "  $f: not qmlformatted"
        FMT_FAIL=$((FMT_FAIL + 1))
    else
        ok  "  $f: formatted"
    fi
done
echo "  $FMT_FAIL file(s) need reformatting"

# --- 4. integration (broker-present VM) ----------------------------

if [ "$NO_INT" = 1 ]; then
    warn "  skipping integration gate (--no-int)"
    INT_RESULT="skipped"
else
    step "integration (qdistro repo + bats VM)"
    # Monorepo: qdistro's own content is the root, one level above qdshell/.
    # Absolute, so the later `cd "$QDISTRO_DIR" && bats "$BATS_FILE"` works.
    QDISTRO_DIR="$(cd "${QDISTRO_DIR:-..}" 2>/dev/null && pwd || echo "${QDISTRO_DIR:-..}")"
    if [ ! -d "$QDISTRO_DIR/tests/integration/vm" ]; then
        warn "  qdistro monorepo root not at $QDISTRO_DIR — skipping integration"
        INT_RESULT="skipped"
    else
        # Drive the qdshell-broker bats from the qdistro side so it
        # reuses the existing helpers + VM bootstrap.
        BATS_FILE="$QDISTRO_DIR/tests/integration/vm/broker-e2e.bats"
        if [ ! -f "$BATS_FILE" ]; then
            warn "  no broker-e2e.bats — skipping integration"
            INT_RESULT="skipped"
        else
            if (cd "$QDISTRO_DIR" && bats "$BATS_FILE"); then
                ok  "  integration bats: PASS"
                INT_RESULT="pass"
            else
                err "  integration bats: FAIL"
                INT_RESULT="fail"
            fi
        fi
    fi
fi

# --- 5. summary ----------------------------------------------------

echo
step "summary"
printf '  qmltest:     %d passed, %d failed across %d files\n' \
    "$QMLTEST_PASS" "$QMLTEST_FAIL" "$QMLTEST_FILES"
printf '  jstest:      %d passed, %d failed across %d files\n' \
    "$JSTEST_PASS" "$JSTEST_FAIL" "$JSTEST_FILES"
printf '  qmllint:     %d warnings, %d errors\n' \
    "$LINT_WARN_COUNT" "$LINT_ERR_COUNT"
printf '  qmlformat:   %d files need reformatting (Services/Qdshell only)\n' \
    "$FMT_FAIL"
printf '  integration: %s\n' "$INT_RESULT"
echo

EXIT=0
if [ "$QMLTEST_FAIL" -gt 0 ]; then
    err "FAIL — qmltest"
    EXIT=1
fi
if [ "$JSTEST_FAIL" -gt 0 ]; then
    err "FAIL — jstest"
    EXIT=1
fi
if [ "$INT_RESULT" = "fail" ]; then
    err "FAIL — integration"
    EXIT=1
fi
if [ "$STRICT" = 1 ] && [ "$LINT_WARN_COUNT" -gt 0 ]; then
    err "FAIL — qmllint warnings (--strict)"
    EXIT=1
fi
if [ "$STRICT" = 1 ] && [ "$FMT_FAIL" -gt 0 ]; then
    err "FAIL — qmlformat (--strict)"
    EXIT=1
fi

if [ "$EXIT" = 0 ]; then
    ok "OK"
fi
exit $EXIT
