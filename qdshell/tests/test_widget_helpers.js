// TEST MIRRORS of pure widget logic extracted from Widgets/*.qml.
// Widgets cannot be instantiated headless (they import qs.Commons /
// qs.Services.UI which are Quickshell singletons requiring a live compositor).
// The pure JS logic below is mirrored verbatim from the QML source.
// tests/test_drift_guard.js asserts these mirrors stay in sync.
//
// Covered widgets:
//   NButton        (Widgets/NButton.qml)         — contentColor binding
//   NComboBox      (Widgets/NComboBox.qml)        — isValueChanged, findIndexByKey
//   NSlider        (Widgets/NSlider.qml)          — snapMode selection
//   NTextInput     (Widgets/NTextInput.qml)       — isValueChanged
//   NKeybindRecorder (Widgets/NKeybindRecorder.qml) — recordingIndex sentinel (-1/>=0)

"use strict";

const assert = require("assert");

// ── NButton: contentColor binding ────────────────────────────────────────────
// Mirrors NButton.qml:
//   readonly property color contentColor: {
//     if (!root.enabled)    return Color.mOnSurfaceVariant;
//     if (root.hovered)     return root.textHoverColor;
//     if (root.outlined)    return root.backgroundColor;
//     return root.textColor;
//   }
function buttonContentColor(enabled, hovered, outlined, backgroundColor, textColor, textHoverColor, onSurfaceVariant) {
    if (!enabled)  return onSurfaceVariant;
    if (hovered)   return textHoverColor;
    if (outlined)  return backgroundColor;
    return textColor;
}

(function testButtonContentColor() {
    assert.strictEqual(
        buttonContentColor(false, false, false, "#primary", "#text", "#hover", "#muted"),
        "#muted", "disabled → onSurfaceVariant");

    assert.strictEqual(
        buttonContentColor(true, true, false, "#primary", "#text", "#hover", "#muted"),
        "#hover", "hovered → textHoverColor");

    assert.strictEqual(
        buttonContentColor(true, false, true, "#primary", "#text", "#hover", "#muted"),
        "#primary", "outlined + not-hovered → backgroundColor");

    assert.strictEqual(
        buttonContentColor(true, false, false, "#primary", "#text", "#hover", "#muted"),
        "#text", "normal → textColor");

    // disabled wins over hovered (enabled check is first)
    assert.strictEqual(
        buttonContentColor(false, true, false, "#primary", "#text", "#hover", "#muted"),
        "#muted", "disabled wins over hovered");

    // hovered wins over outlined
    assert.strictEqual(
        buttonContentColor(true, true, true, "#primary", "#text", "#hover", "#muted"),
        "#hover", "hovered wins over outlined");
})();

// ── NComboBox: isValueChanged ─────────────────────────────────────────────────
// Mirrors NComboBox.qml:
//   readonly property bool isValueChanged:
//     (defaultValue !== undefined) && (currentKey != defaultValue)
// Note: != not !== — intentional so int vs string "30" == 30 (FPS combo).
function comboIsValueChanged(currentKey, defaultValue) {
    return (defaultValue !== undefined) && (currentKey != defaultValue);
}

(function testComboIsValueChanged() {
    assert.strictEqual(comboIsValueChanged("any", undefined), false, "no defaultValue → not changed");
    assert.strictEqual(comboIsValueChanged("top", "top"),     false, "same value → not changed");
    assert.strictEqual(comboIsValueChanged("bottom", "top"),  true,  "different value → changed");

    // != is intentional: "30" != 30 is false (coerces to same)
    assert.strictEqual(comboIsValueChanged("30", 30),  false, "string 30 != int 30 → false (intentional)");
    assert.strictEqual(comboIsValueChanged(30, "30"),  false, "int 30 != string 30 → false (intentional)");

    assert.strictEqual(comboIsValueChanged("", "top"), true, "empty key vs default 'top' → changed");
})();

// ── NComboBox: findIndexByKey ─────────────────────────────────────────────────
// Mirrors NComboBox.qml:
//   function findIndexByKey(key) {
//     for (var i = 0; i < itemCount(); i++) {
//       var item = getItem(i);
//       if (item && item.key === key) return i;
//     }
//     return -1;
//   }
// Note: the real implementation supports both Array models (via root.model[i])
// and ListModel (via root.model.get(i)) through itemCount()/getItem() helpers.
// Both paths return -1 when the key is not found.
function comboFindIndexByKey(model, key) {
    // Array model path (mirrors getItem for Array)
    if (!model) return -1;
    if (Array.isArray(model)) {
        for (var i = 0; i < model.length; i++) {
            if (model[i] && model[i].key === key) return i;
        }
        return -1;
    }
    // ListModel-shaped path (mirrors getItem for model.get)
    if (typeof model.get === "function" && typeof model.count === "number") {
        for (var j = 0; j < model.count; j++) {
            var item = model.get(j);
            if (item && item.key === key) return j;
        }
        return -1;
    }
    return -1;
}

(function testComboFindIndexByKey() {
    var m = [{ key: "top" }, { key: "bottom" }, { key: "left" }];
    assert.strictEqual(comboFindIndexByKey(m, "bottom"), 1, "found at index 1");
    assert.strictEqual(comboFindIndexByKey(m, "top"),    0, "found at first index");
    assert.strictEqual(comboFindIndexByKey(m, "right"), -1, "not found → -1");
    assert.strictEqual(comboFindIndexByKey([],  "top"), -1, "empty array model → -1");
    assert.strictEqual(comboFindIndexByKey(null,"top"), -1, "null model → -1");

    // ListModel-shaped object
    var items = [{ key: "a" }, { key: "b" }, { key: "c" }];
    var listModel = {
        count: items.length,
        get: function(i) { return items[i]; }
    };
    assert.strictEqual(comboFindIndexByKey(listModel, "b"), 1, "ListModel: found at index 1");
    assert.strictEqual(comboFindIndexByKey(listModel, "z"), -1, "ListModel: not found → -1");
})();

// ── NSlider: snapMode selection ───────────────────────────────────────────────
// Mirrors NSlider.qml:
//   snapMode: snapAlways ? Slider.SnapAlways : Slider.SnapOnRelease
// We test the branching (which mode is selected) not the Qt enum integer values.
function sliderSnapMode(snapAlways) {
    return snapAlways ? "SnapAlways" : "SnapOnRelease";
}

(function testSliderSnapMode() {
    assert.strictEqual(sliderSnapMode(true),  "SnapAlways",    "snapAlways:true → SnapAlways");
    assert.strictEqual(sliderSnapMode(false), "SnapOnRelease", "snapAlways:false → SnapOnRelease");
})();

// ── NTextInput: isValueChanged ────────────────────────────────────────────────
// Mirrors NTextInput.qml:
//   readonly property bool isValueChanged:
//     (defaultValue !== undefined) && (text !== defaultValue)
// Note: strict !== (unlike NComboBox which uses !=).
function textInputIsValueChanged(text, defaultValue) {
    return (defaultValue !== undefined) && (text !== defaultValue);
}

(function testTextInputIsValueChanged() {
    assert.strictEqual(textInputIsValueChanged("hello", undefined), false, "no defaultValue → not changed");
    assert.strictEqual(textInputIsValueChanged("hello", "hello"),   false, "same text → not changed");
    assert.strictEqual(textInputIsValueChanged("world", "hello"),   true,  "different text → changed");
    assert.strictEqual(textInputIsValueChanged("", ""),             false, "both empty → not changed");
    assert.strictEqual(textInputIsValueChanged("x", ""),            true,  "nonempty vs empty default → changed");

    // Strict !== means 30 !== "30" → changed (unlike NComboBox which coerces)
    assert.strictEqual(textInputIsValueChanged(30, "30"), true, "strict: int 30 !== string '30' → changed");
})();

// ── NKeybindRecorder: recordingIndex sentinels ────────────────────────────────
// Mirrors NKeybindRecorder.qml comment + code:
//   // -1 = not recording, >= 0 = re-recording at index
//   property int recordingIndex: -1
//
// NOTE: The comment in the QML source mentions "-2 = adding new" but this
// sentinel is NEVER assigned or compared in the production code. The real
// widget renders a fixed Repeater of maxKeybinds slots; clicking an unoccupied
// slot sets recordingIndex = index (>= 0), not -2. Only -1 (idle) and >= 0
// (recording at that slot index) are live values.
function recorderIsIdle(idx) { return idx === -1; }
function recorderIsRecording(idx) { return idx >= 0; }

(function testRecorderSentinels() {
    assert.strictEqual(recorderIsIdle(-1), true,  "recordingIndex=-1 → idle (not recording)");
    assert.strictEqual(recorderIsIdle(0),  false, "recordingIndex=0 → recording, not idle");
    assert.strictEqual(recorderIsIdle(1),  false, "recordingIndex=1 → recording, not idle");

    assert.strictEqual(recorderIsRecording(0),  true,  "index 0 → recording at slot 0");
    assert.strictEqual(recorderIsRecording(1),  true,  "index 1 → recording at slot 1");
    assert.strictEqual(recorderIsRecording(-1), false, "index -1 → not recording");
})();

// ── NKeybindRecorder: maxKeybinds capacity ────────────────────────────────────
// The real widget clamps via newKeybinds.filter(...).slice(0, root.maxKeybinds)
// in _applyKeybind. The Repeater always shows exactly maxKeybinds slots.
// There is no separate canAddMore guard — overflow is silently truncated.
// We test the observable behavior: after slice(0, max) the array length is
// at most max.
(function testRecorderMaxKeybinds() {
    function applyMaxKeybinds(existing, maxKeybinds) {
        // Mirrors the slice in NKeybindRecorder._applyKeybind:
        //   newKeybinds = newKeybinds.filter(k => k !== undefined && k !== "")
        //                            .slice(0, root.maxKeybinds)
        return existing.filter(function(k) { return k !== undefined && k !== ""; })
                       .slice(0, maxKeybinds);
    }

    assert.strictEqual(applyMaxKeybinds([], 2).length, 0, "empty stays empty");
    assert.strictEqual(applyMaxKeybinds(["Ctrl+A"], 2).length, 1, "1 of 2 → length 1");
    assert.strictEqual(applyMaxKeybinds(["Ctrl+A", "Ctrl+B"], 2).length, 2, "2 of 2 → length 2");
    // Over-capacity: truncated to max
    assert.strictEqual(applyMaxKeybinds(["Ctrl+A", "Ctrl+B", "Ctrl+C"], 2).length, 2,
        "3 entries with max=2 → truncated to 2");
    // Undefined/empty entries are filtered out before the slice
    assert.strictEqual(applyMaxKeybinds(["", undefined, "Ctrl+A"], 2).length, 1,
        "empty/undefined entries filtered before length cap");
})();

console.log("widget-helpers: all assertions passed");
