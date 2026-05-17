#!/bin/sh
# install-git-hooks.sh — symlink tracked git hooks from scripts/git-hooks/
# into .git/hooks/. Idempotent: re-run safely.

set -e

ROOT=$(git rev-parse --show-toplevel)
SRC="$ROOT/scripts/git-hooks"
DST="$ROOT/.git/hooks"

if [ ! -d "$SRC" ]; then
    echo "install-git-hooks: $SRC missing" >&2
    exit 1
fi

mkdir -p "$DST"
for hook in "$SRC"/*; do
    name=$(basename "$hook")
    target="$DST/$name"
    # Preserve any existing non-symlink hook with a .bak suffix so the
    # user's manual customisations aren't lost.
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        mv "$target" "$target.bak"
        echo "install-git-hooks: backed up existing $name to $name.bak"
    fi
    ln -sf "$SRC/$name" "$target"
    chmod +x "$SRC/$name"
    echo "install-git-hooks: linked $name"
done
