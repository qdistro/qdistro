// Text processing for the shared bar tooltip. Extracted to plain JS so it can
// be unit-tested under Node (tests/test_tooltip_text.js) while still being
// imported from QML (`import "TooltipText.js" as TooltipText`), mirroring
// Services/UI/LauncherItems.js.
//
// The tooltip body (Modules/Tooltip/Tooltip.qml) renders its `text` as
// Text.RichText so that a "\n" can become a "<br>" line break. That means any
// app-controlled string flowing into the tooltip — focused window titles,
// per-window taskbar/workspace titles, StatusNotifierItem tooltip/name/id,
// MPRIS metadata titles, calendar event summaries — would otherwise be parsed
// as HTML markup in TRUSTED shell chrome. That allows tooltip spoofing and,
// because Qt rich text fetches remote `<img src=...>`, a network beacon from a
// mere taskbar hover. These strings come from silo apps including untrusted
// (tier-3/4) code, so the shell must treat them as plain data.
//
// `processTooltipText` makes every caller safe by default: HTML-escape first,
// THEN translate the explicit "\n" line breaks into "<br>". Escaping before the
// "<br>" substitution is essential — escaping after would re-escape our own
// "<br>" into a literal "&lt;br&gt;". Mirrors LauncherItems.escapeLabel(), the
// existing precedent for exactly this risk.
//
// The grid/rows tooltip path is NOT processed here: those cells render through
// NText with its default Text.PlainText format, so they are already inert.

// HTML-escape a value for safe inclusion in a RichText context. NEVER throws —
// coerces null/undefined/non-strings to a string first (so a numeric or object
// caller cannot crash the shell chrome).
function escapeHtml(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Produce the RichText body for a string-content tooltip: escape, then convert
// newlines to <br>. NEVER throws.
function processTooltipText(content) {
  return escapeHtml(content).replace(/\n/g, "<br>");
}

// CommonJS export for the Node unit test (tests/test_tooltip_text.js). Guarded
// so the QML engine (which has no `module`) ignores it.
if (typeof module !== "undefined" && module.exports) {
  module.exports = { escapeHtml: escapeHtml, processTooltipText: processTooltipText };
}
