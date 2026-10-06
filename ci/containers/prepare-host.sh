#!/usr/bin/env bash
set -euo pipefail
for component in qdchrome-extension qdfirefox-extension; do
    (
        cd "$component"
        key=$(sha256sum package.json package-lock.json; node --version; uname -m)
        if [ -x node_modules/.bin/vitest ] && [ "$(cat node_modules/.qci-deps-key 2>/dev/null || true)" = "$key" ]; then
            echo "$component: dependency cache hit"
            exit 0
        fi
        args=(--prefer-offline)
        case "${QCI_OFFLINE:-0}" in 1|true|yes|on) args=(--offline);; esac
        npm ci "${args[@]}" --cache /tmp/qci-npm
        printf '%s\n' "$key" > node_modules/.qci-deps-key
    )
done
