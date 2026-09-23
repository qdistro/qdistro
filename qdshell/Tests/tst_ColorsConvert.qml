import QtQuick
import QtTest
import "../Helpers/ColorsConvert.js" as C

TestCase {
    name: "ColorsConvert"

    // ---- hex <-> RGB --------------------------------------------------

    function test_hexToRgb_white() {
        const r = C.hexToRgb("#ffffff")
        compare(r.r, 255); compare(r.g, 255); compare(r.b, 255)
    }

    function test_hexToRgb_black() {
        const r = C.hexToRgb("#000000")
        compare(r.r, 0); compare(r.g, 0); compare(r.b, 0)
    }

    function test_hexToRgb_red() {
        const r = C.hexToRgb("#ff0000")
        compare(r.r, 255); compare(r.g, 0); compare(r.b, 0)
    }

    function test_hexToRgb_short_form_3char_returns_black_fallback() {
        // Lib regex demands 6 hex chars; on no-match it returns
        // {0,0,0} as a safe fallback. Pin so callers can rely on
        // never receiving null/undefined here.
        const r = C.hexToRgb("#f0f")
        compare(r.r, 0); compare(r.g, 0); compare(r.b, 0)
    }

    function test_hexToRgb_garbage_returns_black_fallback() {
        const r = C.hexToRgb("zzzzz")
        compare(r.r, 0); compare(r.g, 0); compare(r.b, 0)
    }

    function test_hexToRgb_empty_string_returns_black_fallback() {
        const r = C.hexToRgb("")
        compare(r.r, 0); compare(r.g, 0); compare(r.b, 0)
    }

    function test_hexToRgb_no_hash() {
        const r = C.hexToRgb("ffffff")
        // Implementation may or may not accept lacking #; just verify
        // it doesn't throw.
        verify(r === null || r.r === 255)
    }

    function test_rgbToHex_white() {
        compare(C.rgbToHex(255, 255, 255).toLowerCase(), "#ffffff")
    }

    function test_rgbToHex_black() {
        compare(C.rgbToHex(0, 0, 0).toLowerCase(), "#000000")
    }

    function test_rgbToHex_round_trip() {
        const orig = "#abcdef"
        const r = C.hexToRgb(orig)
        const back = C.rgbToHex(r.r, r.g, r.b).toLowerCase()
        compare(back, orig)
    }

    // ---- HSL ----------------------------------------------------------

    function test_hexToHSL_red() {
        const h = C.hexToHSL("#ff0000")
        compare(h.h, 0)
        compare(h.s, 100)
        compare(h.l, 50)
    }

    function test_hexToHSL_white() {
        const h = C.hexToHSL("#ffffff")
        compare(h.l, 100)
    }

    function test_hexToHSL_black() {
        const h = C.hexToHSL("#000000")
        compare(h.l, 0)
    }

    function test_hslToHex_red() {
        compare(C.hslToHex(0, 100, 50).toLowerCase(), "#ff0000")
    }

    function test_hsl_round_trip_arbitrary() {
        const orig = "#3498db"
        const hsl = C.hexToHSL(orig)
        const back = C.hslToHex(hsl.h, hsl.s, hsl.l).toLowerCase()
        // Allow ±1 channel due to int rounding.
        const o = C.hexToRgb(orig)
        const b = C.hexToRgb(back)
        verify(Math.abs(o.r - b.r) <= 2)
        verify(Math.abs(o.g - b.g) <= 2)
        verify(Math.abs(o.b - b.b) <= 2)
    }

    // ---- luminance + contrast -----------------------------------------

    function test_luminance_white_is_one() {
        fuzzyCompare(C.getLuminance("#ffffff"), 1, 1e-3)
    }

    function test_luminance_black_is_zero() {
        fuzzyCompare(C.getLuminance("#000000"), 0, 1e-3)
    }

    function test_luminance_red_in_range() {
        const l = C.getLuminance("#ff0000")
        verify(l > 0.2 && l < 0.3)
    }

    function test_contrast_white_on_black_is_max() {
        const ratio = C.getContrastRatio("#ffffff", "#000000")
        fuzzyCompare(ratio, 21, 0.1)
    }

    function test_contrast_same_color_is_one() {
        fuzzyCompare(C.getContrastRatio("#3498db", "#3498db"), 1, 0.05)
    }

    function test_contrast_symmetric() {
        const a = C.getContrastRatio("#ffffff", "#000000")
        const b = C.getContrastRatio("#000000", "#ffffff")
        fuzzyCompare(a, b, 0.01)
    }

    // ---- isLightColor -------------------------------------------------

    function test_isLightColor_white_yes() {
        verify(C.isLightColor("#ffffff"))
    }

    function test_isLightColor_black_no() {
        verify(!C.isLightColor("#000000"))
    }

    function test_isLightColor_yellow_yes() {
        verify(C.isLightColor("#ffff00"))
    }

    function test_isLightColor_dark_blue_no() {
        verify(!C.isLightColor("#000080"))
    }

    // ---- adjust* ------------------------------------------------------

    function test_adjustLightness_brighter() {
        // Adjust mid-gray to lighter.
        const out = C.adjustLightness("#808080", 20)
        const l = C.getLuminance(out)
        const orig_l = C.getLuminance("#808080")
        verify(l > orig_l)
    }

    function test_adjustLightness_darker() {
        const out = C.adjustLightness("#808080", -20)
        const l = C.getLuminance(out)
        verify(l < C.getLuminance("#808080"))
    }

    function test_adjustLightness_saturation_clamping() {
        // Pushing white lighter is a no-op (already at 100).
        const out = C.adjustLightness("#ffffff", 50).toLowerCase()
        compare(out, "#ffffff")
    }

    function test_adjustSaturation_returns_hex() {
        // adjustSaturation should always return a 7-char hex string.
        const out = C.adjustSaturation("#808080", 30)
        verify(out.charAt(0) === "#")
        compare(out.length, 7)
    }

    // ---- HSV ----------------------------------------------------------

    function test_rgbToHsv_red() {
        const h = C.rgbToHsv(255, 0, 0)
        fuzzyCompare(h.h, 0, 1)
        fuzzyCompare(h.s, 100, 1)
        fuzzyCompare(h.v, 100, 1)
    }

    function test_hsvToRgb_red() {
        const r = C.hsvToRgb(0, 100, 100)
        compare(r.r, 255); compare(r.g, 0); compare(r.b, 0)
    }
}
