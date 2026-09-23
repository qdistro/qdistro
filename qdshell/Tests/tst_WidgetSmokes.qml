// Widget logic tests — pure JS mirrors exercised under Node.
//
// The 5 most-reused Noctalia/qdshell widgets (NButton, NComboBox, NSlider,
// NTextInput, NKeybindRecorder) all import qs.Commons / qs.Services.UI, which
// are Quickshell singletons that require a live compositor. They cannot be
// instantiated headless under qmltestrunner on the host.
//
// The pure extractable logic (contentColor binding, isValueChanged, etc.) is
// mirrored in tests/test_widget_helpers.js and tested there under Node.
// tests/test_drift_guard.js asserts that those mirrors match the QML source.
//
// Full widget rendering tests (MouseArea, Behavior animations, tooltips,
// focus handling) require the VM and are covered by the UI integration suite.

import QtQuick
import QtTest

TestCase {
    name: "WidgetSmokes"

    // Placeholder: all widget logic tests live in tests/test_widget_helpers.js.
    // See comment at top of file for rationale.
    function test_placeholder_see_test_widget_helpers_js() {
        verify(true, "widget logic tests are in tests/test_widget_helpers.js")
    }
}
