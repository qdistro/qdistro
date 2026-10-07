#!/usr/bin/env bash
# render-profile.sh — substitute @VAR@ placeholders in packaging/agama/*.in
# templates using packaging/env.sh values, writing rendered files to
# packaging/agama/out/ (gitignored).
#
# autoinst*.json.in additionally embed post-install.sh.in (rendered) as the
# @POST_INSTALL_SH@ script content.
#
# Usage: render-profile.sh [template.in ...]   (default: all *.in in this dir)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../env.sh
. "$here/../env.sh"

out=$here/out
mkdir -p "$out"
chmod 700 "$out"   # rendered profiles embed credentials

subst() {
    sed -e "s|@QDISTRO_TW_OSS_URL@|$QDISTRO_TW_OSS_URL|g" \
        -e "s|@QDISTRO_TW_NONOSS_URL@|$QDISTRO_TW_NONOSS_URL|g" \
        -e "s|@QDISTRO_TARGET_OSS_URL@|$QDISTRO_TARGET_OSS_URL|g" \
        -e "s|@QDISTRO_TARGET_NONOSS_URL@|$QDISTRO_TARGET_NONOSS_URL|g" \
        -e "s|@QDISTRO_RPM_REPO_URL@|$QDISTRO_RPM_REPO_URL|g" \
        -e "s|@QDISTRO_TARGET_RPM_REPO_URL@|$QDISTRO_TARGET_RPM_REPO_URL|g" \
        -e "s|@QDISTRO_RPM_KEY_FP@|$QDISTRO_RPM_KEY_FP|g" \
        -e "s|@QDISTRO_ADMIN_PASSWORD@|$QDISTRO_ADMIN_PASSWORD|g" \
        -e "s|@QDISTRO_ROOT_PASSWORD@|$QDISTRO_ROOT_PASSWORD|g" \
        -e "s|@QDISTRO_SNAPSHOT_LABEL@|$QDISTRO_SNAPSHOT_LABEL|g" \
        -e "s|@QDISTRO_VERSION@|$QDISTRO_VERSION|g" \
        -e "s|@QDISTRO_VM_NAME@|${QDISTRO_VM_NAME:-agamatest}|g" \
        -e "s|@QDISTRO_WORK@|${QDISTRO_WORK:-}|g" \
        "$1"
}

# rendered post script, JSON-encoded for embedding
post_json=$(subst "$here/post-install.sh.in" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')

files=("$@")
if [ ${#files[@]} -eq 0 ]; then
    for f in "$here"/*.in "$here"/vm/*.in; do
        [ -e "$f" ] || continue
        [ "$f" = "$here/post-install.sh.in" ] || files+=("$f")
    done
fi

for t in "${files[@]}"; do
    base=$(basename "$t" .in)
    mkdir -p "$out/vm"
    case $t in */vm/*) dest=$out/vm/$base ;; *) dest=$out/$base ;; esac
    subst "$t" > "$dest.tmp"
    if grep -q '@POST_INSTALL_SH@' "$dest.tmp"; then
        POST_JSON=$post_json python3 - "$dest.tmp" "$dest" <<'PYEOF'
import os, sys
doc = open(sys.argv[1]).read()
doc = doc.replace('"@POST_INSTALL_SH@"', os.environ['POST_JSON'])
open(sys.argv[2], 'w').write(doc)
PYEOF
        rm -f "$dest.tmp"
    else
        mv -f "$dest.tmp" "$dest"
    fi
    if grep -q '@[A-Z_]*@' "$dest"; then
        echo "unrendered placeholder left in $dest" >&2; exit 1
    fi
    chmod 600 "$dest"
    echo "rendered $dest"
done
