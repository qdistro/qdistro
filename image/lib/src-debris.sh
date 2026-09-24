#!/bin/bash
# src-debris.sh — host build output and tool caches that must never ship in
# /root/qdistro-src. Sourced by verify-contents.sh (the absence rows) and
# extract-root.sh (whose prune keeps these entries so the rows can see
# them). One list, so the extractor cannot drop what the checklist looks
# for.
#
# image/build.sh copies git's view of the tree, so none of these reach the
# overlay; before that change the sync shipped qdwin/build-qci,
# qdshell/build-qci (host meson builds) and the caches below. config.sh
# removes `build` and `__pycache__` itself at the end of the chroot build,
# so a __pycache__ in the image means something wrote one after it.

# These are the names that HAVE leaked (or are the obvious siblings of
# ones that did), not a complete catalogue of what must not ship.
# shellcheck disable=SC2034  # both lists are read by the sourcing scripts
QDISTRO_SRC_DEBRIS_NAMES=(__pycache__ .mypy_cache .ruff_cache .pytest_cache .hypothesis build-qci)
# Top-level files (depth 1, which the prune never removes).
QDISTRO_SRC_DEBRIS_FILES=(.coverage-report.json .coverage)

# prune_src_tree <dir> — shrink an extracted /root/qdistro-src for the
# checklist: drop everything below the first level, except the tier-3
# helpers (check_link resolves into tier3/) and the debris entries named
# above (kept as empty markers: their contents go, the entry and its
# ancestors stay). Never fails: a directory that still holds a kept entry
# is simply not removed.
prune_src_tree() {
    local src="$1" n expr=()
    [ -d "$src" ] || return 0
    for n in "${QDISTRO_SRC_DEBRIS_NAMES[@]}"; do
        expr+=(${expr[0]:+-o} -name "$n")
    done
    find "$src" -mindepth 2 -not -path "$src/tier3/*" \
        -not \( "${expr[@]}" \) -delete 2>/dev/null || true
}
