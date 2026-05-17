#!/usr/bin/env bash
echo "[install] DEPRECATED: bare-metal-install.sh → use qdistro-bootstrap.sh instead" >&2
exec "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/qdistro-bootstrap.sh" "$@"
