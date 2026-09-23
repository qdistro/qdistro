// NameOwnerParse — pure, side-effect-free parsing of a single
// ``gdbus monitor`` NameOwnerChanged line, extracted from App1Apps.qml
// so the parse/classify logic can be unit-tested headless under Node
// (require("./NameOwnerParse.js")) while still being imported from QML
// (import "NameOwnerParse.js" as NameOwnerParse).
//
// NO Process / Logger / Quickshell access: only string transforms. The
// QML side runs ``gdbus monitor --system --dest org.freedesktop.DBus``
// and feeds each emitted line here; the parsed result tells it which
// well-known name changed owner and whether that name was ACQUIRED
// (new_owner non-empty) or LOST (new_owner empty), so it can decide
// whether to refresh the app inventory, refresh silos, or drop to the
// graceful empty state.
//
// ``gdbus monitor`` emits exactly one line per signal, e.g. (sampled live):
//   /org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged ('org.qdistro.AdminBroker1', '', ':1.42')
//   /org/freedesktop/DBus: org.freedesktop.DBus.NameOwnerChanged (':1.4810', ':1.4810', '')
// The trailing tuple is (name, old_owner, new_owner):
//   * new_owner non-empty  -> the name was just ACQUIRED on the bus.
//   * new_owner empty       -> the name was just LOST.
// An ownership transfer (both old and new owner present) is still an
// acquisition of a (new) owner, so it is classified ACQUIRED.

// Extract (name, old_owner, new_owner) from a NameOwnerChanged line.
// Returns null for any line that is not a well-formed NameOwnerChanged
// signal (wrong signal, no tuple, malformed) so the caller can ignore it
// without crashing.
function parseNameOwnerChanged(line) {
    var s = String(line == null ? "" : line);
    if (s.indexOf("NameOwnerChanged") === -1)
        return null;
    // Capture the three single-quoted fields of the tuple:
    //   ('<name>', '<old_owner>', '<new_owner>')
    var m = s.match(/\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)/);
    if (!m)
        return null;
    var newOwner = m[3];
    return {
        name: m[1],
        oldOwner: m[2],
        newOwner: newOwner,
        // new_owner non-empty == the name now has an owner == acquired.
        acquired: newOwner.length > 0
    };
}

// Classify a monitor line against the two names App1Apps cares about and
// return the action the QML should take. Returns one of:
//   "refresh-apps"  — broker acquired; re-run ListReceivers discovery.
//   "empty-apps"    — broker lost; clear apps + mark broker unreachable.
//   "refresh-silos" — session manager acquired; re-run ListSilos.
//   "ignore"        — unrelated name, broker/session not relevant
//                     (e.g. session manager LOST is not acted on here),
//                     or a malformed / non-matching line.
// brokerName / sessionName are passed in from the QML (root._brokerName /
// root._sessionName) so the well-known names stay defined in one place.
function classifyOwnerChange(line, brokerName, sessionName) {
    var ev = parseNameOwnerChanged(line);
    if (!ev)
        return "ignore";
    if (ev.name === brokerName)
        return ev.acquired ? "refresh-apps" : "empty-apps";
    if (ev.name === sessionName && ev.acquired)
        return "refresh-silos";
    return "ignore";
}

// True iff a ``gdbus monitor --system --dest org.qdistro.AdminBroker1``
// line is the broker's payload-free ReceiversChanged signal. That
// monitor emits, e.g.:
//   /org/qdistro/AdminBroker1: org.qdistro.AdminBroker1.ReceiversChanged ()
// We match the fully-qualified ``<iface>.ReceiversChanged`` token on a
// word boundary rather than a bare substring so a look-alike member
// (e.g. ``...ReceiversChangedExtra``) or an unrelated name can't
// spuriously trigger a refresh. The broker is the only ``--dest`` here,
// so we don't need to re-check the bus name.
function isReceiversChanged(line) {
    var s = String(line == null ? "" : line);
    // org.freedesktop.DBus is a valid name char set (alnum, '_', '.');
    // require ReceiversChanged to be followed by a non-name char (or
    // end of string) so "ReceiversChangedX" does not match.
    return /(^|[^A-Za-z0-9_.])org\.qdistro\.AdminBroker1\.ReceiversChanged([^A-Za-z0-9_]|$)/.test(s);
}

if (typeof module !== "undefined") {
    module.exports = {
        parseNameOwnerChanged: parseNameOwnerChanged,
        classifyOwnerChange: classifyOwnerChange,
        isReceiversChanged: isReceiversChanged,
    };
}
