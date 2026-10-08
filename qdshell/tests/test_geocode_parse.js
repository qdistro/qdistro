// Behavioural test for LocationService.qml's geocode response parser.
// The function is extracted from the production .qml source and executed
// under Node via tests/lib/qmlextract.js — this exercises the real parse
// body, not a re-typed copy. The api.qdshell.dev geocode hop is dead
// upstream; the service now talks to geocoding-api.open-meteo.com, whose
// /v1/search contract this test pins:
//   hit:  {"results":[{"latitude":…,"longitude":…,"name":…,"country":…}]}
//   miss: {"generationtime_ms":…}   (no "results" key at all)

"use strict";

const assert = require("assert");
const QE = require("./lib/qmlextract.js");

var src = QE.read("Services/Location/LocationService.qml");
var parse = QE.compileQmlFunctionWith(src, "parseGeocodeResponse", {});

// Real response shape captured from geocoding-api.open-meteo.com
// /v1/search?name=Berlin&count=1&language=en&format=json (2026-10-08).
const HIT = JSON.stringify({
    results: [{
        id: 2950159, name: "Berlin", latitude: 52.52437,
        longitude: 13.41053, country_code: "DE",
        country: "Germany", admin1: "State of Berlin"
    }],
    generationtime_ms: 0.4812479
});

// A miss returns no "results" key at all (verified live same day).
const MISS = JSON.stringify({ generationtime_ms: 0.8198023 });

(function testHit() {
    // ensures: a real city name resolves to coordinates the weather fetch can use
    var loc = parse(HIT);
    assert.ok(loc !== null, "hit must produce a location");
    assert.strictEqual(loc.latitude, 52.52437);
    assert.strictEqual(loc.longitude, 13.41053);
    assert.strictEqual(loc.name, "Berlin");
    assert.strictEqual(loc.country, "Germany");
})();

(function testMiss() {
    // ensures: an unresolvable name fails closed to null, never to a
    // fabricated coordinate
    assert.strictEqual(parse(MISS), null, "no-results body must parse to null");
    assert.strictEqual(parse(JSON.stringify({ results: [] })), null,
        "empty results array must parse to null");
    assert.strictEqual(parse(JSON.stringify({ results: [{ name: "x" }] })), null,
        "result without latitude must parse to null");
    assert.strictEqual(parse(JSON.stringify({ results: [{ latitude: 5 }] })), null,
        "latitude without longitude must parse to null");
    assert.strictEqual(parse(JSON.stringify({ results: [{ latitude: "x", longitude: 5 }] })), null,
        "non-numeric latitude must parse to null");
})();

(function testMalformed() {
    // ensures: a non-JSON body throws so the XHR handler reports a parse
    // error instead of silently passing garbage downstream
    assert.throws(() => parse("not json"), "malformed body must throw");
    assert.throws(() => parse(""), "empty body must throw");
})();

console.log("test_geocode_parse: all assertions passed");
