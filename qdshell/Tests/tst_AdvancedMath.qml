import QtQuick
import QtTest
import "../Helpers/AdvancedMath.js" as M

TestCase {
    name: "AdvancedMath"

    function test_toRadians_zero() { compare(M.toRadians(0), 0) }
    function test_toRadians_180() { fuzzyCompare(M.toRadians(180), Math.PI, 1e-9) }
    function test_toRadians_360() { fuzzyCompare(M.toRadians(360), Math.PI * 2, 1e-9) }
    function test_toDegrees_zero() { compare(M.toDegrees(0), 0) }
    function test_toDegrees_pi() { fuzzyCompare(M.toDegrees(Math.PI), 180, 1e-9) }
    function test_round_trip_45deg() {
        fuzzyCompare(M.toDegrees(M.toRadians(45)), 45, 1e-9)
    }

    // ---- evaluate() ---------------------------------------------------

    function test_evaluate_simple_addition() {
        compare(M.evaluate("1+2"), 3)
    }

    function test_evaluate_subtraction() {
        compare(M.evaluate("10-3"), 7)
    }

    function test_evaluate_multiplication() {
        compare(M.evaluate("4*5"), 20)
    }

    function test_evaluate_division() {
        compare(M.evaluate("20/4"), 5)
    }

    function test_evaluate_pi_constant() {
        fuzzyCompare(M.evaluate("pi"), Math.PI, 1e-9)
    }

    function test_evaluate_e_constant() {
        fuzzyCompare(M.evaluate("e"), Math.E, 1e-9)
    }

    function test_evaluate_sin_zero() {
        fuzzyCompare(M.evaluate("sin(0)"), 0, 1e-9)
    }

    function test_evaluate_cos_zero() {
        fuzzyCompare(M.evaluate("cos(0)"), 1, 1e-9)
    }

    function test_evaluate_sqrt_4() {
        compare(M.evaluate("sqrt(4)"), 2)
    }

    function test_evaluate_log_is_base10() {
        // Lib defines log() as base-10; ln() as natural log.
        fuzzyCompare(M.evaluate("log(10)"), 1, 1e-9)
        fuzzyCompare(M.evaluate("log(100)"), 2, 1e-9)
    }

    function test_evaluate_ln_is_natural() {
        fuzzyCompare(M.evaluate("ln(e)"), 1, 1e-9)
    }

    function test_evaluate_pow_via_caret_or_func() {
        // The lib supports either ** or pow(); accept either syntax.
        const v = M.evaluate("pow(2,8)")
        compare(v, 256)
    }

    function test_evaluate_invalid_throws() {
        // The lib uses Function() under the hood; nonsense input
        // throws. Pin the throw — caller is responsible for try/catch.
        let threw = false
        try { M.evaluate("not a math expression") }
        catch (e) { threw = true }
        verify(threw)
    }

    function test_evaluate_division_by_zero_throws() {
        // The lib raises "Invalid result" on Infinity to keep
        // calculator UI from displaying a meaningless value.
        let threw = false
        try { M.evaluate("1/0") }
        catch (e) { threw = true }
        verify(threw)
    }

    // ---- formatResult() -----------------------------------------------

    function test_formatResult_integer() {
        compare(M.formatResult(42), "42")
    }

    function test_formatResult_simple_decimal() {
        const s = M.formatResult(3.14)
        verify(s.indexOf("3.14") === 0)
    }

    function test_formatResult_very_small_number_scientific() {
        const s = M.formatResult(1e-10)
        verify(s.indexOf("e") !== -1 || s.indexOf("0.0000000001") !== -1)
    }

    function test_formatResult_negative_zero_is_zero() {
        compare(M.formatResult(-0), "0")
    }

    // ---- getAvailableFunctions() --------------------------------------

    function test_getAvailableFunctions_returns_array() {
        const fns = M.getAvailableFunctions()
        verify(Array.isArray(fns))
        verify(fns.length > 0)
    }

    function test_getAvailableFunctions_mentions_sin() {
        const fns = M.getAvailableFunctions()
        // Returns full doc strings; check for substring match.
        verify(fns.some(s => s.indexOf("sin") !== -1))
    }

    function test_getAvailableFunctions_mentions_sqrt() {
        const fns = M.getAvailableFunctions()
        verify(fns.some(s => s.indexOf("sqrt") !== -1))
    }

    function test_getAvailableFunctions_mentions_log() {
        const fns = M.getAvailableFunctions()
        verify(fns.some(s => s.indexOf("log") !== -1))
    }

    // ---- constants ----------------------------------------------------

    function test_constants_pi() {
        compare(M.constants.PI, Math.PI)
    }

    function test_constants_e() {
        compare(M.constants.E, Math.E)
    }
}
