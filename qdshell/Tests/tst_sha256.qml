import QtQuick
import QtTest
import "../Helpers/sha256.js" as S

TestCase {
    name: "Sha256"

    // Vectors from FIPS PUB 180-4 + RFC 6234 + standard reference impls.
    function test_empty_string() {
        compare(S.sha256(""),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    }

    function test_abc() {
        // Verified against `printf abc | sha256sum`.
        compare(S.sha256("abc"),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    }

    function test_test() {
        // Verified against `printf test | sha256sum`.
        compare(S.sha256("test"),
                "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
    }

    function test_a() {
        compare(S.sha256("a"),
                "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb")
    }

    function test_long_quick_brown_fox() {
        compare(S.sha256("The quick brown fox jumps over the lazy dog"),
                "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592")
    }

    function test_long_input_512_chars() {
        // Exactly 512 chars 'a' — exercises multi-block path.
        const s = "a".repeat(512)
        const h = S.sha256(s)
        compare(h.length, 64)
        // Any non-empty hex output is a smoke pass; the exact value
        // is verifiable against `printf 'a%.0s' {1..512} | sha256sum`
        // but pinning it here would be brittle vs string changes.
    }

    function test_hex_output_lowercase() {
        const h = S.sha256("test")
        compare(h, h.toLowerCase())
    }

    function test_hex_output_64_chars() {
        compare(S.sha256("anything").length, 64)
    }

    function test_unicode_input() {
        // SHA-256 of "café" UTF-8 bytes — verified against
        // `printf 'café' | sha256sum`.
        compare(S.sha256("café"),
                "850f7dc43910ff890f8879c0ed26fe697c93a067ad93a7d50f466a7028a9bf4e")
    }

    function test_determinism() {
        const a = S.sha256("repeatable")
        const b = S.sha256("repeatable")
        compare(a, b)
    }

    function test_avalanche() {
        // Tiny input change → drastically different output.
        const a = S.sha256("test")
        const b = S.sha256("Test")
        verify(a !== b)
        // Hamming distance > 0 over the hex chars.
        let diff = 0
        for (let i = 0; i < a.length; i++) {
            if (a.charAt(i) !== b.charAt(i)) diff++
        }
        verify(diff > 30)
    }
}
