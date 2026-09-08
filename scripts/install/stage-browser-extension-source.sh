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

# The destination is REPLACED, so refuse anything that isn't a plausible
# staging directory. The production caller passes a constant; this guards a
# hand-run with a mistyped or unexpectedly-resolved path. Checks are on the
# CANONICAL path (`realpath -m`, which resolves symlinked parents and `..`
# components without requiring the leaf to exist) — a lexical check would
# pass /usr/share/qdistro/../../../etc.
case "$DEST" in
    /*) ;;
    *) echo "[stage-browser-extension] destination must be absolute: $DEST" >&2; exit 2 ;;
esac
DEST_CANON="$(realpath -m -- "$DEST" 2>/dev/null || true)"
if [ -z "$DEST_CANON" ]; then
    echo "[stage-browser-extension] cannot canonicalize destination: $DEST" >&2
    exit 2
fi
if [ "$(printf '%s' "$DEST_CANON" | tr -cd / | wc -c)" -lt 3 ]; then
    echo "[stage-browser-extension] refusing shallow destination: $DEST_CANON" >&2
    echo "[stage-browser-extension] (expected something like /usr/share/qdistro/browser-extension)" >&2
    exit 2
fi
case "$DEST_CANON" in
    /usr/share/qdistro/*|/tmp/*|/var/tmp/*|"${TMPDIR:-/nonexistent}"/*) ;;
    *)
        echo "[stage-browser-extension] refusing destination outside the staging roots: $DEST_CANON" >&2
        echo "[stage-browser-extension] (production is /usr/share/qdistro/browser-extension; tests use a tmpdir)" >&2
        exit 2 ;;
esac
if [ -L "$DEST_CANON" ]; then
    echo "[stage-browser-extension] refusing symlinked destination: $DEST_CANON" >&2
    exit 2
fi
# Everything below operates on the canonical path.
DEST="$DEST_CANON"

# The source line that proves the gate is closed by default.
#
# NOTE ON WHAT THIS CAN AND CANNOT ESTABLISH. An earlier revision of this
# script also ran a behavioural probe here: it loaded gate.js under node and
# asserted that an empty allowlist denies. That was removed, and deliberately
# not replaced with a "safer sandbox":
#
#   * This script runs as ROOT from the installer. Evaluating JavaScript out
#     of a source checkout at that privilege turns a modified checkout into
#     arbitrary root code execution — a far worse defect than the one the
#     probe was checking for. `new Function(...)` with a substituted
#     `globalThis` is not a sandbox: node's `process` stays reachable.
#   * It was also trivially defeatable in the direction that matters. A gate
#     that called `process.exit(0)` before exporting anything ended the probe
#     with status 0, so a hostile tree could satisfy the probe and still run
#     an open gate in the browser. An assertion an adversary can force to
#     succeed is worse than no assertion, because it is quoted as proof.
#
# So the install-time checks here are STATIC and structural only. They
# establish that the tree ships a gate that will load and that its
# closed-by-default line is present — enough to catch the J11 defect (an
# installed extension with no gate at all) and enough to catch a reverted
# default. They are NOT proof that the gate behaves correctly against a
# hostile tree; nothing an installer can do textually is.
#
# The behavioural proof lives where it can run unprivileged and against a
# known commit:
#   * each extension repo's own vitest suite (`tests/gate.test.js` asserts
#     the empty-allowlist deny and the `*` opt-in), run by the CI host gate;
#   * `tests/unit/test_installed_extension_gate.py::TestRealExtensionRepos`,
#     which loads each repo's real gate.js under node, as the invoking user,
#     and requires it to deny with empty storage;
#   * and the integrity of the tree itself comes from R4's signed source
#     manifest + pinned commit, not from this script reading the code.
GATE_CLOSED_RE='^[[:space:]]*if \(!list\.length\) return false;'

# A source-only extension checkout contains directories and regular files.
# Anything else — symlinks, devices, fifos — is refused rather than copied:
# `cp -r` would preserve a symlink, and a root `chmod` on a staged
# `scripts/build-extension.sh` that is a symlink would then change the mode
# of whatever it points at.
assert_plain_tree() {
    local src="$1" name="$2" bad
    bad="$(find "$src" -path "$src/.git" -prune -o \
                       -path "$src/node_modules" -prune -o \
                       ! -type d ! -type f -print 2>/dev/null | head -5)"
    if [ -n "$bad" ]; then
        echo "[stage-browser-extension] REFUSING to stage $name: source tree contains" >&2
        echo "[stage-browser-extension] symlinks or special files:" >&2
        printf '[stage-browser-extension]   %s\n' $bad >&2
        exit 4
    fi
}

assert_gated() {
    # $1 = source repo checkout, $2 = human name
    local src="$1" name="$2" gate="$1/src/gate.js"
    if [ ! -f "$gate" ] || [ -L "$gate" ]; then
        echo "[stage-browser-extension] REFUSING to stage $name: no src/gate.js in $src" >&2
        echo "[stage-browser-extension] an extension with no module/origin gate is ungated (J11)" >&2
        exit 4
    fi
    # The gate must actually be LOADED. A gate.js that nothing pulls in is
    # not a gate: every privileged call site consults root.qdistroGate, so an
    # unloaded gate is an absent one. Comments are stripped first so a mere
    # mention of the path does not satisfy this.
    # NB: captured into a variable rather than piped into `grep -q`. Under
    # `set -o pipefail`, grep -q exits at the first match, SIGPIPEs sed, and
    # the pipeline reports failure — which would read as "gate not loaded"
    # for exactly the large, correct files it is meant to accept.
    local wiring
    wiring="$(_uncommented "$src/src/background.js" "$src/manifest.json" \
                           "$src/manifest.chromium.json")"
    if ! printf '%s' "$wiring" | grep -qF "src/gate.js"; then
        echo "[stage-browser-extension] REFUSING to stage $name: src/gate.js is not loaded by" >&2
        echo "[stage-browser-extension] the background wiring or the manifest — it would never run" >&2
        exit 4
    fi
    if ! grep -qE "$GATE_CLOSED_RE" "$gate"; then
        echo "[stage-browser-extension] REFUSING to stage $name: $gate does not close the" >&2
        echo "[stage-browser-extension] origin allowlist by default (J11) — expected" >&2
        echo "[stage-browser-extension]   if (!list.length) return false;" >&2
        exit 4
    fi
}

# Concatenate the given files with // and /* */ comments stripped. Used so
# the gate-is-loaded check cannot be satisfied by a comment or a docstring.
_uncommented() {
    local f
    for f in "$@"; do
        [ -f "$f" ] || continue
        sed -e 's://.*::' "$f"
    done
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
    assert_plain_tree "$repo_src" "$dest_name"
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
# This is two renames, not one atomic exchange (renameat2/RENAME_EXCHANGE is
# not reachable from portable shell), so the window between them is handled
# explicitly: if the second rename fails, the previous tree is put back. The
# alternative — leaving $DEST absent — would turn a transient failure into a
# host with no extension source and no record of why.
old=""
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    old="$(mktemp -d "$(dirname "$DEST")/.$(basename "$DEST").old.XXXXXX")"
    mv -- "$DEST" "$old/tree"
fi
if ! mv -- "$staging" "$DEST"; then
    echo "[stage-browser-extension] staging swap failed; restoring the previous tree" >&2
    if [ -n "$old" ] && [ -e "$old/tree" ]; then
        rm -rf -- "$DEST"
        mv -- "$old/tree" "$DEST" \
            || echo "[stage-browser-extension] RESTORE FAILED: previous tree left at $old/tree" >&2
        rm -rf -- "$old"
    fi
    exit 5
fi
trap - EXIT
rm -rf -- "$staging"
[ -n "$old" ] && rm -rf -- "$old"

if [ "${#found_src[@]}" -eq 0 ]; then
    echo "[stage-browser-extension] WARN: no browser-extension source installed under $DEST" >&2
    echo "[stage-browser-extension] (both extension repos are optional; browser integration is" >&2
    echo "[stage-browser-extension]  unavailable until one is checked out and this is re-run —" >&2
    echo "[stage-browser-extension]  see doc/browser.md, \"Firefox extension artifacts\")" >&2
fi
exit 0
