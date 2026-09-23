const assert = require("assert");
const NOP = require("../Services/Qdistro/NameOwnerParse.js");

// These tests pin the event-driven discovery wiring in App1Apps.qml: a
// long-running `gdbus monitor --system --dest org.freedesktop.DBus` feeds
// one line per signal into NameOwnerParse.classifyOwnerChange(), whose
// verdict decides whether the launcher refreshes its app inventory,
// refreshes silos, or drops to the graceful empty state. The QML calls the
// SAME functions exercised here, so a regression in this classification is
// a regression in launcher discovery.

// The well-known names App1Apps tracks (mirrors root._brokerName /
// root._sessionName in App1Apps.qml).
const BROKER = "org.qdistro.AdminBroker1";
const SESSION = "org.qdistro.SessionManager1";

// Line shape locked against live `gdbus monitor --system --dest
// org.freedesktop.DBus` output, sampled as:
//   /org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged (':1.4810', '', ':1.4810')
//   /org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged (':1.4810', ':1.4810', '')
function monitorLine(name, oldOwner, newOwner) {
  return "/org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged ('" +
    name + "', '" + oldOwner + "', '" + newOwner + "')";
}

// ── parser extracts (name, old_owner, new_owner) and acquired flag ──
(function testParseFields() {
  const ev = NOP.parseNameOwnerChanged(monitorLine(BROKER, "", ":1.42"));
  assert.deepStrictEqual(ev, {
    name: BROKER, oldOwner: "", newOwner: ":1.42", acquired: true
  }, "parses name/old/new and flags acquired on non-empty new_owner");
})();

// ── broker ACQUIRED -> refresh the app inventory ──
// ensures: when the admin broker appears on the bus, the launcher re-runs
// ListReceivers discovery instead of showing a stale/empty section.
(function testBrokerAcquired() {
  const line = monitorLine(BROKER, "", ":1.42");
  const ev = NOP.parseNameOwnerChanged(line);
  assert.strictEqual(ev.name, BROKER);
  assert.strictEqual(ev.acquired, true, "non-empty new_owner == acquired");
  assert.strictEqual(
    NOP.classifyOwnerChange(line, BROKER, SESSION), "refresh-apps",
    "broker acquired must trigger an app refresh");
})();

// ── broker LOST -> graceful empty state ──
// ensures: when the broker leaves the bus, the launcher drops to the empty
// state (clear apps + brokerReachable=false) rather than freezing stale rows.
(function testBrokerLost() {
  const line = monitorLine(BROKER, ":1.42", "");
  const ev = NOP.parseNameOwnerChanged(line);
  assert.strictEqual(ev.acquired, false, "empty new_owner == lost");
  assert.strictEqual(
    NOP.classifyOwnerChange(line, BROKER, SESSION), "empty-apps",
    "broker lost must drop to the graceful empty state");
})();

// ── session manager ACQUIRED -> refresh silos, NOT apps ──
// ensures: the SessionManager1 owner change refreshes the silo chips and
// does not get misrouted into the app-inventory refresh path.
(function testSessionAcquired() {
  const line = monitorLine(SESSION, "", ":1.7");
  const verdict = NOP.classifyOwnerChange(line, BROKER, SESSION);
  assert.strictEqual(verdict, "refresh-silos",
    "session manager acquired must refresh silos");
  assert.notStrictEqual(verdict, "refresh-apps",
    "session manager change must NOT trigger an app refresh");
})();

// ── unrelated bus name -> ignored, no action ──
// ensures: arbitrary bus traffic (the common case on a busy system bus)
// never spuriously refreshes the launcher.
(function testUnrelatedName() {
  const cases = [
    monitorLine(":1.4810", "", ":1.4810"),         // anonymous connection
    monitorLine("org.freedesktop.NetworkManager", "", ":1.9"),
    monitorLine("org.qdistro.AdminBroker2", "", ":1.9"), // similar but not it
    monitorLine("org.qdistro.SessionManager11", "", ":1.9"),
  ];
  cases.forEach(function (line) {
    assert.strictEqual(
      NOP.classifyOwnerChange(line, BROKER, SESSION), "ignore",
      "unrelated name must be ignored: " + line);
  });
})();

// ── session manager LOST -> ignored (only acquired triggers a silo refresh) ──
// ensures: a session-manager owner-LOST is not acted on (the inline QML
// only refreshes silos on acquire), so behavior matches the runtime code.
(function testSessionLostIgnored() {
  const line = monitorLine(SESSION, ":1.7", "");
  assert.strictEqual(
    NOP.classifyOwnerChange(line, BROKER, SESSION), "ignore",
    "session manager lost is not acted on");
})();

// ── malformed / non-matching lines -> ignored, never crash ──
// ensures: monitor banner lines, truncated tuples, and junk never throw and
// never spuriously act.
(function testMalformed() {
  const junk = [
    "",
    null,
    undefined,
    "Monitoring signals from all objects owned by org.freedesktop.DBus",
    "The name org.freedesktop.DBus is owned by org.freedesktop.DBus",
    // right signal name but no tuple
    "/org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged",
    // truncated tuple (only two fields)
    "/org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged ('" + BROKER + "', '')",
    // a different signal entirely
    "/org/freedesktop/DBus: org.freedesktop.DBus.NameAcquired ('" + BROKER + "')",
    "garbage NameOwnerChanged not a tuple at all",
  ];
  junk.forEach(function (line) {
    assert.strictEqual(NOP.parseNameOwnerChanged(line), null,
      "malformed line parses to null: " + String(line));
    assert.strictEqual(
      NOP.classifyOwnerChange(line, BROKER, SESSION), "ignore",
      "malformed line is ignored: " + String(line));
  });
})();

// ── ownership transfer (both old and new owner present) -> acquired ──
// ensures: a name handed from one connection to another (old_owner AND
// new_owner non-empty) is treated as an acquisition (a live owner exists),
// so the launcher refreshes rather than going empty.
(function testOwnershipTransfer() {
  const line = monitorLine(BROKER, ":1.10", ":1.99");
  const ev = NOP.parseNameOwnerChanged(line);
  assert.strictEqual(ev.oldOwner, ":1.10");
  assert.strictEqual(ev.newOwner, ":1.99");
  assert.strictEqual(ev.acquired, true,
    "non-empty new_owner is acquired even with an old owner present");
  assert.strictEqual(
    NOP.classifyOwnerChange(line, BROKER, SESSION), "refresh-apps",
    "ownership transfer of the broker triggers a refresh");
})();

// ── ReceiversChanged: matches the real broker signal line ──
// ensures: a `gdbus monitor --system --dest org.qdistro.AdminBroker1`
// line carrying the broker's payload-free ReceiversChanged is recognised
// so the launcher re-runs ListReceivers when a receiver registers /
// unregisters inside a living silo. Line shape sampled from gdbus:
//   /org/qdistro/AdminBroker1: org.qdistro.AdminBroker1.ReceiversChanged ()
(function testReceiversChangedMatches() {
  const line =
    "/org/qdistro/AdminBroker1: org.qdistro.AdminBroker1.ReceiversChanged ()";
  assert.strictEqual(NOP.isReceiversChanged(line), true,
    "real ReceiversChanged line must match");
})();

// ── ReceiversChanged: rejects NameOwnerChanged + unrelated lines ──
// ensures: the dedicated ReceiversChanged matcher does not fire on the
// other monitor's NameOwnerChanged traffic, on look-alike member names,
// or on junk — so we never refresh on the wrong signal.
(function testReceiversChangedRejects() {
  const rejects = [
    // NameOwnerChanged of the broker (the OTHER monitor's signal).
    monitorLine(BROKER, "", ":1.42"),
    // Look-alike member that merely starts with ReceiversChanged.
    "/org/qdistro/AdminBroker1: org.qdistro.AdminBroker1.ReceiversChangedExtra ()",
    // Right member name but a different (look-alike) interface.
    "/org/qdistro/AdminBroker1: org.qdistro.AdminBroker2.ReceiversChanged ()",
    // Unrelated broker signal.
    "/org/qdistro/AdminBroker1: org.qdistro.AdminBroker1.RulesReloaded ()",
    // Junk / monitor banner / empties.
    "",
    null,
    undefined,
    "Monitoring signals from all objects owned by org.qdistro.AdminBroker1",
    "garbage line with no signal at all",
  ];
  rejects.forEach(function (line) {
    assert.strictEqual(NOP.isReceiversChanged(line), false,
      "must NOT match: " + String(line));
  });
})();

console.log("app1apps-nameowner: all assertions passed");
