#!/usr/bin/env bats
#
# Host-only checks for the qsu-under-Enforcing driver
# (tests/integration/vm/s54-qsu-enforcing.sh), whose live run needs an
# enforcing VM over SSH (tests/integration/vm/qsu-enforcing.bats).
#
# The AVC scan must hand ausearch the start DATE and TIME as two separate
# arguments: ausearch parses a single "date time" word as a time, refuses it,
# and the driver (rightly) turns a failed audit query into a FAILURE -- so a
# one-word --start fails the required case on every healthy guest.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    DRIVER="$REPO_ROOT/tests/integration/vm/s54-qsu-enforcing.sh"
    FN="$BATS_TEST_TMPDIR/fn.sh"
    sed -n '/^ausearch_since() {$/,/^}$/p' "$DRIVER" > "$FN"
    [ -s "$FN" ]
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    # A stand-in that records its argv one word per line, and the locale.
    cat > "$BATS_TEST_TMPDIR/bin/ausearch" <<'SH'
#!/bin/bash
printf 'LC_ALL=%s\n' "${LC_ALL:-}"
printf '%s\n' "$@"
SH
    chmod +x "$BATS_TEST_TMPDIR/bin/ausearch"
}

@test "s54: the AVC scan passes --start DATE TIME as two arguments, C locale" {
    run env PATH="$BATS_TEST_TMPDIR/bin:$PATH" bash -c '. "$1"; ausearch_since 1791972000' _ "$FN"
    [ "$status" -eq 0 ]
    [ "${lines[0]}" = "LC_ALL=C" ]
    [ "${lines[1]}" = "-m" ]
    [ "${lines[2]}" = "AVC,USER_AVC" ]
    [ "${lines[3]}" = "--start" ]
    # date (C locale %x: MM/DD/YY) and time (%T: HH:MM:SS), separately
    [[ "${lines[4]}" =~ ^[0-9]{2}/[0-9]{2}/[0-9]{2}$ ]]
    [[ "${lines[5]}" =~ ^[0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]
    [ "${#lines[@]}" -eq 6 ]
    [ "${lines[4]}" = "$(LC_ALL=C date -d @1791972000 +%x)" ]
    [ "${lines[5]}" = "$(LC_ALL=C date -d @1791972000 +%T)" ]
}

@test "s54: the scan is the driver's only ausearch call, and a failed query is a failure" {
    [ "$(grep -c 'ausearch_since "$BASELINE_TS"' "$DRIVER")" -eq 1 ]
    run grep -nE '^[[:space:]]*[^#"]*ausearch -' "$DRIVER"
    [ "${#lines[@]}" -eq 1 ]
    [[ "$output" == *'LC_ALL=C ausearch -m AVC,USER_AVC --start "$d" "$t"'* ]]
    grep -q "zero denials NOT established" "$DRIVER"
    grep -q "<no matches>" "$DRIVER"
}
