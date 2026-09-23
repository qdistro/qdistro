pragma Singleton
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons

// spec/10 Phase-1 — local clipboard policy loader.
// Loads from (in precedence order):
//   1. $QDSHELL_CLIPBOARD_POLICY (override for tests / dev)
//   2. /etc/qdistro/clipboard-policy.json     (system policy)
//   3. /etc/qdistro/clipboard-policy.yaml     (system policy, YAML form)
// Phase-1 ships the JSON variant only — QML has native `JSON.parse`
// but no YAML parser; the YAML form is reserved for a future C++
// helper. The file format documented below matches the YAML schema in
// 04-compositor-clipboard.md §"Policy language" exactly, just spelled
// as JSON.
// Schema (JSON form):
//   {
//     "clipboard": [
//       {
//         "from": "<silo-glob>",     // required
//         "to":   "<silo-glob>"      // required; may be a list
//                 | ["<silo-glob>", "<silo-glob>"],
//         "mime_types": ["text/*", "image/*"],  // optional; default = ["*"]
//         "verdict": "allow" | "deny" | "prompt"  // required
//       },
//       ...
//     ]
//   }
// Silo globs: `*` matches any silo. Otherwise exact match. (fnmatch
// glob support is Phase-2; Phase-1 covers the common cases.)
// MIME globs: a `text/*` rule matches any selection whose MIME LIST
// contains at least one type matching the glob. We bias to allow if
// ANY listed mime matches — Wayland clipboards always carry
// `text/plain` alongside richer types and we'd otherwise block
// legitimate text-only transfers.
// Decision: first matching rule wins. If no rule matches, the verdict
// is `deny` with `reason=default-deny` — this is the fall-through the
// spec mandates.
// TODO(track-04-phase-2): YAML support via a small C++ helper in
// qml-plugin/ that round-trips YAML → JSON before exposing to QML.
// Same schema; just a different on-disk representation.
// TODO(track-04-phase-2): live reload via FileView.watchChanges. Today
// the policy is loaded once at startup; admin changes require a
// shell reload.
Singleton {
    id: root

    // Public read — array of rule objects. Empty array means "no policy
    // loaded yet" or "fall through to default-deny".
    property var rules: []
    property bool loaded: false

    // Where we ended up loading from. Empty if no file was found.
    property string loadedFrom: ""

    function load() {
        const override = Quickshell.env("QDSHELL_CLIPBOARD_POLICY") || "";
        const candidates = [];
        if (override.length > 0) {
            candidates.push(override);
        }
        candidates.push("/etc/qdistro/clipboard-policy.json");
        // YAML variant logged-but-skipped at Phase-1.
        candidates.push("/etc/qdistro/clipboard-policy.yaml");
        for (let i = 0; i < candidates.length; i++) {
            const p = candidates[i];
            if (p.endsWith(".yaml")) {
                // Phase-1: YAML not parsed. Log a notice if the file exists
                // so admins know why their YAML rules aren't taking effect.
                _yamlProbe.path = "file://" + p;
                continue;
            }
            _jsonView.path = "file://" + p;
            // FileView is reactive; onLoaded fires asynchronously. Break on
            // first candidate — fallback handled in onLoadFailed.
            root.loadedFrom = p;
            return;
        }
        root.loaded = true;  // empty policy → default-deny everywhere
        Logger.i("ClipboardPolicy", "no policy file; default-deny for all cross-silo transfers");
    }

    // Consult: returns { verdict, reason }. Phase-1 verdict ∈
    // {"allow", "deny"} — prompt collapses to deny for the user-visible
    // behaviour until the affordance ships.
    function consult(srcSilo, dstSilo, mimeList) {
        if (!root.loaded) {
            return {
                "verdict": "deny",
                "reason": "policy-not-loaded"
            };
        }
        for (let i = 0; i < root.rules.length; i++) {
            const rule = root.rules[i];
            if (!_siloMatch(rule.from, srcSilo)) {
                continue;
            }
            if (!_siloMatchAny(rule.to, dstSilo)) {
                continue;
            }
            if (!_mimeMatchAny(rule.mime_types, mimeList)) {
                continue;
            }
            const v = rule.verdict || "deny";
            if (v === "prompt") {
                // TODO(track-04-phase-2): surface the "Request transfer"
                // affordance. Today we deny so secrets don't leak silently.
                return {
                    "verdict": "deny",
                    "reason": "prompt-collapsed:rule#" + i
                };
            }
            return {
                "verdict": v,
                "reason": "rule#" + i
            };
        }
        return {
            "verdict": "deny",
            "reason": "default-deny"
        };
    }

    // -- private --------------------------------------------------------
    FileView {
        id: _jsonView
        printErrors: false
        onLoaded: {
            try {
                const data = JSON.parse(_jsonView.text());
                const arr = (data && data.clipboard) || [];
                if (!Array.isArray(arr)) {
                    throw new Error("`clipboard` must be an array of rules");
                }
                // Validate every rule has the required fields; drop bad rules
                // loudly rather than silently mis-applying policy.
                const cleaned = [];
                for (let i = 0; i < arr.length; i++) {
                    const r = arr[i];
                    if (!r || typeof r !== "object") {
                        Logger.w("ClipboardPolicy", "rule #" + i + " is not an object; skipping");
                        continue;
                    }
                    if (!r.from || !r.to || !r.verdict) {
                        Logger.w("ClipboardPolicy", "rule #" + i + " missing from/to/verdict; skipping");
                        continue;
                    }
                    if (["allow", "deny", "prompt"].indexOf(r.verdict) < 0) {
                        Logger.w("ClipboardPolicy", "rule #" + i + " verdict='" + r.verdict + "' invalid; skipping");
                        continue;
                    }
                    cleaned.push(r);
                }
                root.rules = cleaned;
                root.loaded = true;
                Logger.i("ClipboardPolicy", "loaded", cleaned.length, "rules from", root.loadedFrom);
            } catch (e) {
                Logger.e("ClipboardPolicy", "parse failed for", root.loadedFrom, ":", e);
                root.rules = [];
                root.loaded = true;  // default-deny
            }
        }
        onLoadFailed: function (error) {
            Logger.d("ClipboardPolicy", "no JSON policy at", root.loadedFrom, "(", error, "); default-deny");
            root.rules = [];
            root.loaded = true;
            root.loadedFrom = "";
        }
    }

    FileView {
        id: _yamlProbe
        printErrors: false
        onLoaded: {
            Logger.w("ClipboardPolicy", "YAML policy at", _yamlProbe.path, "found but Phase-1 ships JSON only; ignoring.", "TODO(track-04-phase-2): wire YAML parser.");
        }
        onLoadFailed: function (error) {}
    }

    function _siloMatch(pattern, value) {
        if (pattern === "*" || pattern === undefined || pattern === null) {
            return true;
        }
        return pattern === value;
    }

    function _siloMatchAny(pattern, value) {
        if (Array.isArray(pattern)) {
            for (let i = 0; i < pattern.length; i++) {
                if (_siloMatch(pattern[i], value)) {
                    return true;
                }
            }
            return false;
        }
        return _siloMatch(pattern, value);
    }

    function _mimeGlobMatch(glob, mime) {
        if (!glob || glob === "*" || glob === "*/*") {
            return true;
        }
        if (glob.indexOf("*") < 0) {
            return glob === mime;
        }
        // Limited glob: "<prefix>/*" and "*/<suffix>". Anchor both ends.
        const star = glob.indexOf("*");
        const before = glob.substring(0, star);
        const after = glob.substring(star + 1);
        return mime.indexOf(before) === 0 && mime.length >= before.length + after.length && mime.substring(mime.length - after.length) === after;
    }

    function _mimeMatchAny(globs, mimeList) {
        if (!globs || globs.length === 0) {
            return true;  // unspecified → match any mime
        }
        const list = Array.isArray(globs) ? globs : [globs];
        for (let i = 0; i < list.length; i++) {
            for (let j = 0; j < mimeList.length; j++) {
                if (_mimeGlobMatch(list[i], mimeList[j])) {
                    return true;
                }
            }
        }
        return false;
    }
}
