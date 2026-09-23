// Pure launcher-item logic, extracted from the Launcher bar widget /
// LauncherSettings so it can be unit-tested under Node
// (see tests/test_launcher_items.js) while still being imported from QML
// (`import "LauncherItems.js" as LauncherItems`).
//
// A "launcher item" is a user-defined quick-launch button with three
// fields:
//   - name:    display label / tooltip (UNTRUSTED user input)
//   - icon:    Tabler icon name (UNTRUSTED user input)
//   - command: shell command line to run (the user's OWN command, akin to
//              a .desktop Exec= — executing it is intended)
//
// Everything here operates ONLY on plain arrays/strings. There is NO access
// to Quickshell / Icons / Settings singletons — the QML side reads those and
// passes the resulting primitives in (and does the final exec). This keeps the
// functions deterministic and testable.
//
// SECURITY — this is the load-bearing part:
//
//  * `name` and `icon` are attacker-influenceable labels. They MUST NEVER end
//    up in the executed argv. `buildExec()` builds the command argv from the
//    `command` field ONLY; name/icon are not interpolated anywhere near it.
//
//  * `command` is executed via `["sh", "-lc", command]` — the SAME pattern as
//    Services/Qdwin/Qdwin.qml spawn() and CustomButton.qml. The command string
//    is passed as a SINGLE argv element, so the shell evaluates it exactly
//    once. We do NOT wrap it in extra quotes (that would either double-evaluate
//    or break legitimate commands like `foo && bar`); instead we rely on the
//    argv boundary: nothing outside the `command` field can splice into it.
//
//  * `sanitizeIcon()` strips an icon name down to a conservative
//    [a-z0-9-] charset so a crafted icon value cannot break out of any
//    string context (e.g. a QML icon: binding or a tooltip). An icon that
//    fails sanitization collapses to "" and the QML layer falls back to the
//    default launcher icon. (The QML layer additionally checks the sanitized
//    name against the real Icons set; this module only guarantees the charset.)

// Conservative icon-name charset. Tabler icon names are lowercase
// alphanumerics joined by single hyphens (e.g. "folder-open"). Anything
// else is rejected.
var ICON_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
var MAX_ICON_LEN = 64;

function asString(v) {
  if (typeof v === "string")
    return v;
  if (v === undefined || v === null)
    return "";
  return String(v);
}

// Reduce an arbitrary icon value to a safe Tabler-style name, or "" if it
// cannot be represented safely. NEVER throws.
function sanitizeIcon(icon) {
  var s = asString(icon).trim().toLowerCase();
  if (s.length === 0 || s.length > MAX_ICON_LEN)
    return "";
  // Collapse any run of disallowed chars; this both normalizes separators
  // and removes shell/markup metacharacters.
  s = s.replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").replace(/-+/g, "-");
  if (s.length === 0 || !ICON_RE.test(s))
    return "";
  return s;
}

// Trim a label-ish string and collapse internal newlines (a launcher name is
// a single-line label / tooltip).
function sanitizeName(name) {
  return asString(name).replace(/[\r\n\t]+/g, " ").trim();
}

// HTML-escape a label for safe use in a RichText context, so a name like
// "<b>x" or "a<br>b" is rendered literally rather than parsed as markup. The
// stored name itself stays verbatim (the settings editor shows it as a plain
// text field). NEVER throws.
//
// NOTE: the bar tooltip no longer needs this at its use-site — it HTML-escapes
// content centrally (Services/UI/TooltipText.js), so the launcher passes the
// raw name through and the tooltip escapes once. Kept as a general utility for
// any code that builds a RichText string itself.
function escapeLabel(name) {
  return asString(name)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// The command is stored verbatim (the user's own command). We only trim outer
// whitespace; we never rewrite its contents.
function sanitizeCommand(command) {
  return asString(command).trim();
}

// A launcher item is VALID iff it has a non-empty name AND a non-empty
// command after sanitization. (Icon is optional — it falls back to a default.)
function isValidItem(item) {
  if (!item || typeof item !== "object")
    return false;
  return sanitizeName(item.name).length > 0
      && sanitizeCommand(item.command).length > 0;
}

// Make a defensive, normalized copy of one item. Always returns an object with
// {name, icon, command}; invalid icons collapse to "".
function normalizeItem(item) {
  var e = (item && typeof item === "object") ? item : {};
  return {
    "name": sanitizeName(e.name),
    "icon": sanitizeIcon(e.icon),
    "command": sanitizeCommand(e.command),
  };
}

// Normalize + filter an entire items list: drop invalid entries (missing
// name/command) and return a fresh array of normalized items. Order preserved.
function normalizeList(list) {
  var out = [];
  var arr = Array.isArray(list) ? list : [];
  for (var i = 0; i < arr.length; i++) {
    var e = normalizeItem(arr[i]);
    if (e.name.length === 0 || e.command.length === 0)
      continue;
    out.push(e);
  }
  return out;
}

// Serialize the items list to a JSON string suitable for persisting in the
// per-instance widget settings. Always normalized first.
function serialize(list) {
  return JSON.stringify(normalizeList(list));
}

// Parse a previously-serialized items list. Accepts either a JSON string or an
// already-decoded array (QML may hand us either). Never throws — bad input
// yields [].
function deserialize(data) {
  if (Array.isArray(data))
    return normalizeList(data);
  if (typeof data !== "string" || data.length === 0)
    return [];
  try {
    var parsed = JSON.parse(data);
    return normalizeList(parsed);
  } catch (e) {
    return [];
  }
}

// Append a new item. Returns a NEW normalized list. An invalid item (no
// name/command) is rejected and the original (normalized) list is returned.
function addItem(list, item) {
  var out = normalizeList(list);
  if (!isValidItem(item))
    return out;
  out.push(normalizeItem(item));
  return out;
}

// Remove the item at `index`. Out-of-range index is a no-op. Returns a NEW
// normalized list.
function removeItem(list, index) {
  var out = normalizeList(list);
  if (typeof index !== "number" || index < 0 || index >= out.length)
    return out;
  out.splice(index, 1);
  return out;
}

// Replace the item at `index` with `item`. If `item` is invalid or the index
// is out of range, the list is returned unchanged (normalized). Returns a NEW
// list.
function updateItem(list, index, item) {
  var out = normalizeList(list);
  if (typeof index !== "number" || index < 0 || index >= out.length)
    return out;
  if (!isValidItem(item))
    return out;
  out[index] = normalizeItem(item);
  return out;
}

// Move the item at `from` to `to` (reorder). Clamps/validates indices; an
// invalid move is a no-op. Returns a NEW normalized list.
function moveItem(list, from, to) {
  var out = normalizeList(list);
  var n = out.length;
  if (typeof from !== "number" || typeof to !== "number")
    return out;
  if (from < 0 || from >= n || to < 0 || to >= n || from === to)
    return out;
  var moved = out.splice(from, 1)[0];
  out.splice(to, 0, moved);
  return out;
}

// Convenience reorder helpers used by the settings UI.
function moveUp(list, index) {
  return moveItem(list, index, index - 1);
}
function moveDown(list, index) {
  return moveItem(list, index, index + 1);
}

// Build the SAFE exec argv for an item. The command is the SOLE source of the
// executed string; name/icon are not consulted here at all. Returns null when
// the item has no usable command (caller must not exec). The argv mirrors
// Qdwin.spawn() — the command is one argv element, evaluated by the shell
// exactly once. Because it is a discrete argv element, no other field (and
// nothing outside `command`) can splice into the executed string.
function buildExec(item) {
  var cmd = sanitizeCommand(item && item.command);
  if (cmd.length === 0)
    return null;
  return ["sh", "-lc", cmd];
}

var api = {
  sanitizeIcon: sanitizeIcon,
  sanitizeName: sanitizeName,
  escapeLabel: escapeLabel,
  sanitizeCommand: sanitizeCommand,
  isValidItem: isValidItem,
  normalizeItem: normalizeItem,
  normalizeList: normalizeList,
  serialize: serialize,
  deserialize: deserialize,
  addItem: addItem,
  removeItem: removeItem,
  updateItem: updateItem,
  moveItem: moveItem,
  moveUp: moveUp,
  moveDown: moveDown,
  buildExec: buildExec,
};

if (typeof module !== "undefined") {
  module.exports = api;
}
