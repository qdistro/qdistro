#!/usr/bin/env bash
# Create per-component source tarballs for rpmbuild from a qdistro checkout.
# Usage: make-sources.sh [qdistro-checkout]   (default: this worktree)
set -euo pipefail
repo=${1:-$(cd "$(dirname "$0")/../.." && pwd)}
out=$(cd "$(dirname "$0")" && pwd)/rpmbuild/SOURCES
mkdir -p "$out"

# name:version:subdir[+subdir...]  (each subdir is rooted at name-version/<subdir>/)
specs="
qdwin:0.1.0:qdwin
qdistro-daemons:0.1.0:daemons
qdistro-presentation:1.0.0:sdk/presentation
qdgreeter:0.1.0:qdgreeter
qdlocker:0.1.0:qdlocker
qterminator:0.1.0:qdterm
qfileman:0.1.0:qdfileman
qnotebook:0.0.1:qnotebook
qdbrowser:0.1.0:qdbrowser
qdshell:0.1.0:qdshell
qdistro-session:0.1.0:deploy
qdistro-admin:0.1.0:admin_app+cli+tui+deploy
qdistro-browser-bridge:0.1.0:browser_bridge+browser_daemons+qdchrome-extension+qdfirefox-extension+qdbrowser
"

# Loose spec Source files whose canonical copies live in the monorepo
# (referenced as SourceN in specs/; rpmbuild needs them in SOURCES/).
cp -f "$repo/scripts/install/qdistro-session-provision.sh" "$out/"
cp -f "$repo/scripts/install/harden-compositor-vt.sh" "$out/"

for line in $specs; do
    name=${line%%:*}; rest=${line#*:}
    ver=${rest%%:*}; subs=${rest#*:}
    subs=${subs//+/ }
    transforms=()
    if [ "$(wc -w <<< "$subs")" = 1 ]; then
        # single dir: rename the tree root to name-version (original layout)
        transforms+=(--transform "s,^${subs},${name}-${ver},")
    else
        for sub in $subs; do
            transforms+=(--transform "s,^${sub},${name}-${ver}/${sub##*/},")
        done
    fi
    for sub in $subs; do
        [ -d "$repo/$sub" ] || { echo "missing $repo/$sub" >&2; exit 1; }
    done
    excludes=(--exclude='*__pycache__*' --exclude='.pytest_cache'
              --exclude='node_modules')
    for sub in $subs; do
        # Build output lives at <component>/build{,-*} (meson per AGENTS.md).
        # Do NOT exclude '*/build' at any depth: quickshell-vendored's
        # src/src/build is an upstream *source* dir that add_subdirectory
        # needs — stripping it broke the qdistro-quickshell rpmbuild.
        excludes+=(--exclude="${sub}/build" --exclude="${sub}/build-*"
                   --exclude="${sub}/builddir" --exclude="${sub}/builddir-*"
                   --exclude="${sub}/dist" --exclude="${sub}/dist-*")
    done
    # Nested output dirs the component-root rules cannot see: the vendored
    # wrappers default to <vendored>/src/build{,-*}. A '<vendored>/build-*/*'
    # pattern guts stray root-level build dirs (build-rpm, build-prod)
    # without eating the build-*.sh source scripts — 'build-*' unqualified
    # would also match those files. Empty dir entries are harmless.
    excludes+=(--exclude='qdshell/quickshell-vendored/src/build'
               --exclude='qdshell/quickshell-vendored/src/build-*'
               --exclude='qdshell/quickshell-vendored/build-*/*'
               --exclude='qdwin/libweston-vendored/src/build'
               --exclude='qdwin/libweston-vendored/src/build-*'
               --exclude='qdwin/libweston-vendored/build-*/*'
               --exclude='qdwin/libweston-vendored/prefix'
               --exclude='qdwin/libweston-vendored/prefix-*')
    tar -C "$repo" --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner \
        "${excludes[@]}" \
        "${transforms[@]}" \
        -czf "$out/${name}-${ver}.tar.gz" $subs
    echo "wrote $out/${name}-${ver}.tar.gz"
done
