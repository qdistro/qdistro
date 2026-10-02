#!/usr/bin/env bats
# Host-only: four first-party chrome adapters follow one snapshot in
# separate offscreen processes. Boot workers keep two windows and a
# Preferences/Settings dialog open. No VM, no root, no /var/lib/qdistro
# writes. Live compositor GUI and enforcing tier launches remain a
# separate claim.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    PROBE="$REPO/tests/integration/vm/probes/presentation-four-apps.py"
}

@test "four apps follow publish, late join, malformed keep, fonts, delete keep, enabled:false" {
    run env -u QDISTRO_PRESENTATION_FILE -u PYTHONPATH \
        QT_QPA_PLATFORM=offscreen PYTHONSAFEPATH=1 \
        python3 "$PROBE"
    echo "$output"
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}

@test "four-app probe never calls _reload(" {
    if grep -n '_reload(' "$PROBE"; then
        echo "probe calls _reload(" >&2
        return 1
    fi
}

@test "four-app boot workers construct two windows and a preferences dialog" {
    grep -q 'from qfileman.window import FileManagerWindow' "$PROBE"
    grep -q 'from qterminator.window import MainWindow' "$PROBE"
    grep -q 'from qdbrowser.window import MainWindow' "$PROBE"
    grep -q 'from qnotebook.window import MainWindow' "$PROBE"
    grep -q 'from qfileman.preferences import PreferencesDialog' "$PROBE"
    grep -q 'from qterminator.preferences import PreferencesDialog' "$PROBE"
    grep -q 'from qdbrowser.preferences import PreferencesDialog' "$PROBE"
    grep -q 'from qnotebook.settings_dialog import SettingsDialog' "$PROBE"
    grep -q 'with_windows=True' "$PROBE"
    grep -qF 'size_matches(widget, FONT_SIZE)' "$PROBE"
    grep -qF 'FONT_SIZE = 11.0 * FONT_UI_SCALE' "$PROBE"
    grep -qF 'FONT_UI_SCALE = 1.25' "$PROBE"
    grep -qF 'size=FONT_SIZE_TOKEN' "$PROBE"
    grep -qF 'generation=gen_fonts' "$PROBE"
    grep -qF 'parsed.get("gen") != generation' "$PROBE"
}

@test "assert_chrome rejects stale generation" {
    run env -u QDISTRO_PRESENTATION_FILE -u PYTHONPATH \
        PYTHONSAFEPATH=1 python3 "$PROBE" --assert-chrome-self-test
    echo "$output"
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}
