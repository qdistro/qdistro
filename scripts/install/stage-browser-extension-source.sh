#!/bin/bash
# stage-browser-extension-source.sh — lay down the WebExtension SOURCE
# trees a user builds and loads by hand, and refuse to lay down an
# ungated one.
#
#   usage: stage-browser-extension-source.sh <dest-dir> [source-root]
#
# <dest-dir>     where the trees go (the installer passes
#                /usr/share/qdistro/browser-extension). It is REPLACED:
#                see the upgrade note below.
# [source-root]  directory holding the qdchrome-extension /
#                qdfirefox-extension checkouts. Defaults to
#                $QDISTRO_EXTENSION_SRC_ROOT, then the usual source
#                roots. Both repos are optional (mirroring
#                qdistro-bootstrap.sh's optional fetch): an absent repo
#                warns, it does not fail.
#
# Why this is a separate script, and fail-closed:
#
# J11 ("close the extension origin allowlist by default") landed in the
# qdchrome-extension repo. But install-browser-bridge-for-vm.sh shipped
# `qdistro/browser_bridge/extension/` — a tree vendored inside qdistro
# that was an abandoned fork of the pre-split Phase-9a extension. That
# fork never grew the module/origin gate (`src/gate.js`), so it had no
# allowlist at all, and it was the only extension any install path
# actually laid down: the hardened extension lived in a repo nothing
# packaged. The fork is deleted; the maintained, gated repos are the
# only source now.
#
# The gate assertion below is the packaging assertion that was missing.
# A tree whose origin allowlist is not closed by default is NEVER
# staged — the install aborts instead. That is the property
# `tests/unit/test_installed_extension_gate.py` drives this script to
# prove, which is why the logic lives in its own file rather than inline
# in an installer that needs root and writes /usr.
#
# Ordering matters and is deliberate:
#   1. validate EVERY present source first,
#   2. build the replacement tree in a temp dir beside the destination,
#   3. only then swap it in.
# A malformed checkout therefore cannot leave the destination purged,
# half-populated, or holding a mix of old and new trees.
set -euo pipefail

DEST="${1:?usage: stage-browser-extension-source.sh <dest-dir> [source-root]}"
SRC_ROOT="${2:-${QDISTRO_EXTENSION_SRC_ROOT:-}}"

# The destination is wiped, so refuse anything that isn't a plausible
# staging directory. The production caller passes a constant; this
# guards a hand-run with a mistyped or unexpectedly-resolved path.
case "$DEST" in
    /*) ;;
    *) echo "[stage-browser-extension] destination must be absolute: $DEST" >&2; exit 2 ;;
esac
case "$DEST" in
    */.|*/..|*/) echo "[stage-browser-extension] refusing relative-suffix destination: $DEST" >&2; exit 2 ;;
esac
if [ "$(printf '%s' "$DEST" | tr -cd / | wc -c)" -lt 3 ]; then
    echo "[stage-browser-extension] refusing shallow destination: $DEST" >&2
    echo "[stage-browser-extension] (expected something like /usr/share/qdistro/browser-extension)" >&2
    exit 2
fi
if [ -L "$DEST" ]; then
    echo "[stage-browser-extension] refusing symlinked destination: $DEST" >&2
    exit 2
fi

# The source line that proves the gate is closed by default. A textual
# check alone is weak (it can match a dead function) and brittle (an
# equivalent refactor would be rejected), so it is only the first of
# three checks — see assert_gated below.
GATE_CLOSED_RE='^[[:space:]]*if \(!list\.length\) return false;'

# Behavioural probe: load gate.js the way the extension does (a bare
# `self` carrying qdistroApi) with EMPTY storage, and require that
# isOriginAllowed denies. This is what actually matters; the grep above
# only catches the case where node is unavailable. Node is present on a
# qdistro image and in CI, but the probe is skipped rather than fatal if
# it is missing, so the textual check stays as the floor.
probe_gate_denies() {
    local gate="$1"
    command -v node >/dev/null 2>&1 || return 2
    node --input-type=module -e '
const fs = await import("node:fs");
const gate = process.argv[1];
const scope = {
  qdistroApi: {
    storage: {
      // Empty stored config: no modules, no origin_allowlist. Both the
      // callback and Promise shapes, since the two repos differ.
      local: { get: (_k, cb) => { if (cb) cb({}); return Promise.resolve({}); } },
      onChanged: { addListener: () => {} },
    },
  },
};
const src = fs.readFileSync(gate, "utf8");
new Function("self", "globalThis", src)(scope, scope);
await new Promise((r) => setTimeout(r, 0));
const g = scope.qdistroGate;
if (!g || typeof g.isOriginAllowed !== "function") {
  console.error("gate.js did not export qdistroGate.isOriginAllowed");
  process.exit(1);
}
for (const url of ["https://anything.example/", "http://plain.test/", ""]) {
  if (g.isOriginAllowed(url) !== false) {
    console.error(`isOriginAllowed(${JSON.stringify(url)}) allowed with an empty allowlist`);
    process.exit(1);
  }
}
' "$gate" >/dev/null 2>&1
}

assert_gated() {
    # $1 = source repo checkout, $2 = human name
    local src="$1" name="$2" gate="$1/src/gate.js"
    if [ ! -f "$gate" ]; then
        echo "[stage-browser-extension] REFUSING to stage $name: no src/gate.js in $src" >&2
        echo "[stage-browser-extension] an extension with no module/origin gate is ungated (J11)" >&2
        exit 4
    fi
    # The gate must actually be LOADED. A gate.js that nothing pulls in
    # is not a gate: every privileged call site consults
    # root.qdistroGate, so an unloaded gate is an absent one.
    if ! grep -rqF "src/gate.js" "$src/src" "$src/manifest.json" \
            "$src/manifest.chromium.json" 2>/dev/null; then
        echo "[stage-browser-extension] REFUSING to stage $name: src/gate.js is not referenced by" >&2
        echo "[stage-browser-extension] the background wiring or the manifest — it would never load" >&2
        exit 4
    fi
    if ! grep -qE "$GATE_CLOSED_RE" "$gate"; then
        echo "[stage-browser-extension] REFUSING to stage $name: $gate does not close the" >&2
        echo "[stage-browser-extension] origin allowlist by default (J11) — expected" >&2
        echo "[stage-browser-extension]   if (!list.length) return false;" >&2
        exit 4
    fi
    local probe_rc=0
    probe_gate_denies "$gate" || probe_rc=$?
    case "$probe_rc" in
        0) ;;
        2) echo "[stage-browser-extension] WARN: node unavailable; $name gate checked textually only" >&2 ;;
        *)
            echo "[stage-browser-extension] REFUSING to stage $name: with an EMPTY origin allowlist," >&2
            echo "[stage-browser-extension] $gate still allows origins (J11). The gate is not closed" >&2
            echo "[stage-browser-extension] by default at runtime, whatever the source text says." >&2
            exit 4 ;;
    esac
}

copy_source() {
    # $1 = source repo checkout, $2 = destination dir
    local src="$1" dest="$2"
    install -d -m 0755 "$dest"
    cp -r "$src/." "$dest/"
    # Build outputs and dev trees are not source and must not be handed
    # to a user as if they were: dist/ in particular could be a stale
    # build made BEFORE the gate landed.
    rm -rf "$dest/.git" "$dest/node_modules" "$dest/coverage" "$dest/dist"
    if [ -f "$dest/scripts/build-extension.sh" ]; then
        chmod 0755 "$dest/scripts/build-extension.sh"
    fi
}

# ---- 1. resolve + validate every present source ---------------------

declare -a found_src=() found_name=()
for pair in qdchrome-extension:chromium qdfirefox-extension:firefox; do
    repo="${pair%%:*}"
    dest_name="${pair##*:}"
    repo_src=""
    for cand in \
        "${SRC_ROOT:+$SRC_ROOT/$repo}" \
        "/opt/qdistro-src/$repo" \
        "/root/qdistro-src/$repo"; do
        [ -n "$cand" ] || continue
        if [ -f "$cand/package.json" ] && [ -d "$cand/src" ]; then
            repo_src="$cand"; break
        fi
    done
    if [ -z "$repo_src" ]; then
        echo "[stage-browser-extension] WARN: $repo not checked out; nothing staged for it" >&2
        continue
    fi
    assert_gated "$repo_src" "$dest_name"
    found_src+=("$repo_src")
    found_name+=("$dest_name")
done

# ---- 2. build the replacement tree out of the way -------------------

install -d -m 0755 "$(dirname "$DEST")"
staging="$(mktemp -d "$(dirname "$DEST")/.$(basename "$DEST").new.XXXXXX")"
cleanup() { rm -rf "$staging"; }
trap cleanup EXIT
chmod 0755 "$staging"

for i in "${!found_src[@]}"; do
    copy_source "${found_src[$i]}" "$staging/${found_name[$i]}"
    echo "[stage-browser-extension] staged gated extension source: ${found_name[$i]}"
done

# ---- 3. swap it in --------------------------------------------------
#
# A pre-J11 host has the ungated fork sitting at $DEST. Deleting a tree
# from the repo does not uninstall it, so the destination is REPLACED,
# not merged into — an in-place upgrade must not leave the ungated
# extension loadable.
old=""
if [ -e "$DEST" ]; then
    old="$(mktemp -d "$(dirname "$DEST")/.$(basename "$DEST").old.XXXXXX")"
    mv "$DEST" "$old/tree"
fi
mv "$staging" "$DEST"
trap - EXIT
[ -n "$old" ] && rm -rf "$old"

if [ "${#found_src[@]}" -eq 0 ]; then
    echo "[stage-browser-extension] WARN: no browser-extension source installed under $DEST" >&2
    echo "[stage-browser-extension] (both extension repos are optional; browser integration is" >&2
    echo "[stage-browser-extension]  unavailable until one is checked out and this is re-run —" >&2
    echo "[stage-browser-extension]  see doc/browser.md, \"Firefox extension artifacts\")" >&2
fi
exit 0
