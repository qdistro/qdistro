#!/usr/bin/env bash
# render-profile.sh — substitute @VAR@ placeholders in packaging/agama/*.in
# templates using packaging/env.sh values, writing rendered files to
# packaging/agama/out/ (gitignored).
#
# Substitution is literal (python str.replace), so values may contain
# characters that are special to sed (| & \) or JSON (quotes): in *.json.in
# the `"@VAR@"` token is replaced with a json.dumps() serialization.
# autoinst*.json.in additionally embed post-install.sh.in (rendered) as the
# script content.
#
# Usage: render-profile.sh [template.in ...]   (default: all *.in in this dir)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../env.sh
. "$here/../env.sh"

out=$here/out
mkdir -p "$out"
chmod 700 "$out"   # rendered profiles embed credentials

files=("$@")
if [ ${#files[@]} -eq 0 ]; then
    for f in "$here"/*.in "$here"/vm/*.in; do
        [ -e "$f" ] || continue
        [ "$f" = "$here/post-install.sh.in" ] || files+=("$f")
    done
fi

export QDISTRO_TW_OSS_URL QDISTRO_TW_NONOSS_URL \
    QDISTRO_TARGET_OSS_URL QDISTRO_TARGET_NONOSS_URL \
    QDISTRO_RPM_REPO_URL QDISTRO_TARGET_RPM_REPO_URL \
    QDISTRO_RPM_KEY_FP QDISTRO_ADMIN_PASSWORD QDISTRO_ROOT_PASSWORD \
    QDISTRO_SNAPSHOT_LABEL QDISTRO_VERSION QDISTRO_VM_NAME QDISTRO_WORK

python3 - "$here" "$out" "${files[@]}" <<'PYEOF'
import json, os, re, sys

here, out, files = sys.argv[1], sys.argv[2], sys.argv[3:]
mapping = {k: os.environ[k] for k in (
    'QDISTRO_TW_OSS_URL', 'QDISTRO_TW_NONOSS_URL',
    'QDISTRO_TARGET_OSS_URL', 'QDISTRO_TARGET_NONOSS_URL',
    'QDISTRO_RPM_REPO_URL', 'QDISTRO_TARGET_RPM_REPO_URL',
    'QDISTRO_RPM_KEY_FP', 'QDISTRO_ADMIN_PASSWORD',
    'QDISTRO_ROOT_PASSWORD', 'QDISTRO_SNAPSHOT_LABEL',
    'QDISTRO_VERSION', 'QDISTRO_VM_NAME', 'QDISTRO_WORK')}

def subst(text, json_mode):
    for k, v in mapping.items():
        # In JSON templates the placeholder appears as a full string token
        # "@VAR@" — serialize the value so quotes/backslashes stay valid.
        if json_mode:
            text = text.replace('"@%s@"' % k, json.dumps(v))
        else:
            text = text.replace('@%s@' % k, v)
    return text

post = subst(open(os.path.join(here, 'post-install.sh.in')).read(), False)

for t in files:
    base = os.path.basename(t)[:-3]
    json_mode = t.endswith('.json.in')
    doc = subst(open(t).read(), json_mode)
    if json_mode:
        doc = doc.replace('"@POST_INSTALL_SH@"', json.dumps(post))
        json.loads(doc)   # fail loudly on a broken render
    if re.search(r'@[A-Z_]+@', doc):
        sys.exit('unrendered placeholder left in ' + t)
    dest_dir = os.path.join(out, 'vm') if '/vm/' in t else out
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, base)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)   # enforce on existing files too — rendered profiles embed credentials
    with os.fdopen(fd, 'w') as f:
        f.write(doc)
    print('rendered', dest)
PYEOF
