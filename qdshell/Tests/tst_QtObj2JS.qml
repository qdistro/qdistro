import QtQuick
import QtTest
import "../Helpers/QtObj2JS.js" as Q

TestCase {
    name: "QtObj2JS"

    // ---- primitive passthrough ----------------------------------------

    function test_null_passthrough() {
        compare(Q.qtObjectToPlainObject(null), null)
    }

    function test_undefined_passthrough() {
        compare(Q.qtObjectToPlainObject(undefined), undefined)
    }

    function test_number_passthrough() {
        compare(Q.qtObjectToPlainObject(42), 42)
    }

    function test_string_passthrough() {
        compare(Q.qtObjectToPlainObject("hello"), "hello")
    }

    function test_bool_true_passthrough() {
        compare(Q.qtObjectToPlainObject(true), true)
    }

    function test_bool_false_passthrough() {
        compare(Q.qtObjectToPlainObject(false), false)
    }

    function test_zero_passthrough() {
        compare(Q.qtObjectToPlainObject(0), 0)
    }

    function test_empty_string_passthrough() {
        compare(Q.qtObjectToPlainObject(""), "")
    }

    // ---- arrays -------------------------------------------------------

    function test_empty_array() {
        const out = Q.qtObjectToPlainObject([])
        verify(Array.isArray(out))
        compare(out.length, 0)
    }

    function test_array_of_primitives() {
        const out = Q.qtObjectToPlainObject([1, 2, 3])
        compare(out.length, 3)
        compare(out[0], 1)
        compare(out[1], 2)
        compare(out[2], 3)
    }

    function test_array_of_strings() {
        const out = Q.qtObjectToPlainObject(["a", "b"])
        compare(out[0], "a")
        compare(out[1], "b")
    }

    function test_nested_arrays() {
        const out = Q.qtObjectToPlainObject([[1, 2], [3, 4]])
        compare(out[0][0], 1)
        compare(out[1][1], 4)
    }

    // ---- objects ------------------------------------------------------

    function test_simple_object() {
        const out = Q.qtObjectToPlainObject({ a: 1, b: 2 })
        compare(out.a, 1)
        compare(out.b, 2)
    }

    function test_nested_object() {
        const out = Q.qtObjectToPlainObject({
            outer: { inner: { deep: "value" } }
        })
        compare(out.outer.inner.deep, "value")
    }

    function test_object_with_array_value() {
        const out = Q.qtObjectToPlainObject({ items: [1, 2, 3] })
        compare(out.items.length, 3)
        compare(out.items[1], 2)
    }

    function test_array_with_object_elements() {
        const out = Q.qtObjectToPlainObject([{ a: 1 }, { a: 2 }])
        compare(out[0].a, 1)
        compare(out[1].a, 2)
    }

    // ---- JSON round-trip ---------------------------------------------

    function test_json_serializable_output() {
        const input = {
            name: "qdshell",
            version: 1,
            list: [1, 2, 3],
            nested: { flag: true }
        }
        const plain = Q.qtObjectToPlainObject(input)
        // Output should be JSON.stringify-able without errors.
        const json = JSON.stringify(plain)
        verify(json.indexOf("qdshell") !== -1)
        verify(json.indexOf("[1,2,3]") !== -1)
    }

    function test_json_round_trip_preserves_shape() {
        const input = { a: 1, b: { c: [10, 20] } }
        const plain = Q.qtObjectToPlainObject(input)
        const round = JSON.parse(JSON.stringify(plain))
        compare(round.a, 1)
        compare(round.b.c.length, 2)
        compare(round.b.c[0], 10)
    }
}
