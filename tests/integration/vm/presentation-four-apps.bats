#!/usr/bin/env bats
# Host-only: four first-party chrome adapters follow one snapshot in
# separate offscreen processes. No VM, no root, no /var/lib/qdistro writes.
# Live compositor GUI and enforcing tier launches remain a separate claim.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    PROBE="$REPO/tests/integration/vm/probes/presentation-four-apps.py"
}

@test "four apps follow publish A then B without _reload and keep B after delete" {
    run env -u QDISTRO_PRESENTATION_FILE -u PYTHONPATH \
        QT_QPA_PLATFORM=offscreen PYTHONSAFEPATH=1 \
        python3 "$PROBE"
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}
