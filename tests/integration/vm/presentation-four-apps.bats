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
    grep -q 'assert_chrome(boot, window=COLOR_C, family=FONT_FAMILY_TOKEN)' "$PROBE"
}
