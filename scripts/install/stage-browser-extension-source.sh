#!/bin/bash
# stage-browser-extension-source.sh — lay down the WebExtension SOURCE
# trees a user builds and loads by hand, and refuse to lay down an
# ungated one.
#
#   usage: stage-browser-extension-source.sh <dest-dir> [source-root]
#
# <dest-dir>     where the trees go (the installer passes
#                /usr/share/qdistro/browser-extension). It is PURGED
#                first: see the upgrade note below.
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
# A tree with no `src/gate.js`, or one whose origin allowlist is not
# closed by default, is NEVER staged — the install aborts instead. That
# is the property `tests/unit/test_installed_extension_gate.py` drives
# this script to prove, which is why the logic lives in its own file
# rather than inline in an installer that needs root and writes /usr.
set -euo pipefail

DEST="${1:?usage: stage-browser-extension-source.sh <dest-dir> [source-root]}"
SRC_ROOT="${2:-${QDISTRO_EXTENSION_SRC_ROOT:-}}"

# The source line that proves the gate is closed by default. Pinned on
# code, not on a comment, so reverting the fix while keeping the prose
# still fails the install.
GATE_CLOSED_RE='^[[:space:]]*if \(!list\.length\) return false;'

stage_one() {
    # $1 = source repo checkout, $2 = destination subdir name
    local src="$1" name="$2"
    local gate="$src/src/gate.js" dest="$DEST/$2"
    if [ ! -f "$gate" ]; then
        echo "[stage-browser-extension] REFUSING to stage $name: no src/gate.js in $src" >&2
        echo "[stage-browser-extension] an extension with no module/origin gate is ungated (J11)" >&2
        exit 4
    fi
    if ! grep -qE "$GATE_CLOSED_RE" "$gate"; then
        echo "[stage-browser-extension] REFUSING to stage $name: $gate does not close the" >&2
        echo "[stage-browser-extension] origin allowlist by default (J11) — expected" >&2
        echo "[stage-browser-extension]   if (!list.length) return false;" >&2
        exit 4
    fi
    install -d -m 0755 "$dest"
    cp -r "$src/." "$dest/"
    # Build outputs and dev trees are not source and must not be handed
    # to a user as if they were: dist/ in particular could be a stale
    # build made BEFORE the gate landed.
    rm -rf "$dest/.git" "$dest/node_modules" "$dest/coverage" "$dest/dist"
    if [ -f "$dest/scripts/build-extension.sh" ]; then
        chmod 0755 "$dest/scripts/build-extension.sh"
    fi
    echo "[stage-browser-extension] staged gated extension source: $name -> $dest"
}

# A pre-J11 host has the ungated fork sitting at $DEST. Deleting a tree
# from the repo does not uninstall it, so purge the destination first —
# an in-place upgrade must not leave the ungated extension loadable.
install -d -m 0755 "$DEST"
find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

staged_any=0
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
    stage_one "$repo_src" "$dest_name"
    staged_any=1
done

if [ "$staged_any" -eq 0 ]; then
    echo "[stage-browser-extension] WARN: no browser-extension source installed under $DEST" >&2
    echo "[stage-browser-extension] (both extension repos are optional; browser integration is" >&2
    echo "[stage-browser-extension]  unavailable until one is checked out and this is re-run —" >&2
    echo "[stage-browser-extension]  see doc/browser-extension-install.md)" >&2
fi
