#!/usr/bin/env bash
# Self-hosted nix binary cache for qdistro (attic), one per host user.
#
#   nix/attic/serve.sh start    # start (first run: config, secret, cache)
#   nix/attic/serve.sh stop
#   nix/attic/serve.sh status   # endpoint, public key, cache info
#   nix/attic/serve.sh env      # shell lines: ATTIC_* + NIX_EXTRA_CONF
#
# atticd runs from the same podman nix store nix/flake-check.sh uses
# (volume nix-$USER-store, mounted read-only), on host networking at
# 127.0.0.1:$ATTIC_PORT (default 30100 + uid % 10000, a range disjoint from
# the qd-repo-cache proxy's 20000 + uid % 10000). State — sqlite db, chunk storage,
# the JWT secret, an admin token — lives in $ATTIC_STATE
# (default ~/.local/share/qdistro-attic, mode 0700). The cache is named
# `qdistro` and is public-read; pushing needs the token (push.sh).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
user=$(id -un)
port=${ATTIC_PORT:-$((30100 + $(id -u) % 10000))}
state=${ATTIC_STATE:-$HOME/.local/share/qdistro-attic}
name=attic-$user
volume=${NIX_CHECK_VOLUME:-nix-$user-store}
image=${NIX_CHECK_IMAGE:-docker.io/nixos/nix:latest}
cache=qdistro
endpoint=http://127.0.0.1:$port

nixc() { NIX_CHECK_KEEP=1 "$here/nix/flake-check.sh" "$@"; }

attic_paths() {
  # Pinned to the flake's own nixpkgs, built into the shared store.
  nixc build --no-link --print-out-paths \
    --inputs-from "$here" nixpkgs#attic-server nixpkgs#attic-client
}

wait_up() {
  for _ in $(seq 50); do
    curl -fsS -o /dev/null "$endpoint/" 2>/dev/null && return 0
    curl -sS -o /dev/null -w '%{http_code}' "$endpoint/" 2>/dev/null | grep -q '^[1-5]' && return 0
    sleep 0.2
  done
  echo "attic: $endpoint did not come up" >&2
  podman logs --tail 30 "$name" >&2 || true
  return 1
}

start() {
  umask 077   # secret, token and config are credentials
  mkdir -p "$state/storage"
  chmod 0700 "$state"
  mapfile -t paths < <(attic_paths)
  server=${paths[0]} client=${paths[1]}
  # Store paths live in the podman volume, not on the host: test them there.
  podman exec "nix-$user" test -x "$server/bin/atticd" \
    || { server=${paths[1]}; client=${paths[0]}; }
  printf '%s\n%s\n' "$server" "$client" > "$state/paths"

  if [ ! -s "$state/jwt-secret" ]; then
    if [ -s "$state/server.toml" ]; then   # state from an earlier layout
      sed -n 's/^token-hs256-secret-base64 = "\(.*\)"$/\1/p' "$state/server.toml" > "$state/jwt-secret"
    fi
    [ -s "$state/jwt-secret" ] || openssl rand -base64 64 | tr -d '\n' > "$state/jwt-secret"
  fi
  secret=$(cat "$state/jwt-secret")
  # Regenerated every start, so a port change takes effect.
  cat > "$state/server.toml" <<EOF
listen = "127.0.0.1:$port"
api-endpoint = "$endpoint/"

[database]
url = "sqlite:///state/server.db?mode=rwc"

[storage]
type = "local"
path = "/state/storage"

[chunking]
nar-size-threshold = 65536
min-size = 16384
avg-size = 65536
max-size = 262144

[compression]
type = "zstd"

[garbage-collection]
interval = "0s"

[jwt.signing]
token-hs256-secret-base64 = "$secret"
EOF

  # The listen port is baked into the running server; recreate on change.
  if podman container exists "$name" &&
     [ "$(podman inspect -f '{{index .Config.Labels "qdistro.attic-port"}}' "$name")" != "$port" ]; then
    podman rm -f -t 5 "$name" >/dev/null
  fi
  if ! podman container exists "$name"; then
    podman run -d --name "$name" --label "qdistro.attic-port=$port" --network host --security-opt label=disable \
      -v "$volume:/nix:ro" -v "$state:/state" \
      --entrypoint "$server/bin/atticd" "$image" \
      -f /state/server.toml --mode monolithic >/dev/null
  else
    podman start "$name" >/dev/null
  fi
  wait_up

  if [ ! -s "$state/admin.token" ]; then
    podman exec "$name" "$server/bin/atticadm" -f /state/server.toml \
      make-token --sub "$user-admin" --validity "10y" \
      --pull "$cache" --push "$cache" --delete "$cache" \
      --create-cache "$cache" --configure-cache "$cache" \
      --configure-cache-retention "$cache" --destroy-cache "$cache" \
      > "$state/admin.token"
    chmod 0600 "$state/admin.token"
  fi

  # Create the public cache once; record its signing public key.
  if [ ! -s "$state/public-key" ]; then
    client_run cache create --public "$cache" >/dev/null 2>&1 || true   # exists on reruns
    client_run cache info "$cache" 2>&1 | sed -n 's/.*Public Key: *//p' > "$state/public-key"
    [ -s "$state/public-key" ] || { echo "attic: no public key for $cache" >&2; exit 1; }
  fi
  echo "attic: $endpoint/$cache  key $(cat "$state/public-key")"
}

# Run the attic client in the flake-check container; it reaches the server
# on 127.0.0.1 because that container also uses host networking.
client_run() {
  client=$(sed -n 2p "$state/paths")
  podman container exists "nix-$user" || nixc --version >/dev/null
  # The token goes over stdin, never argv (/proc/<pid>/cmdline is
  # world-readable); the client config is written 0600.
  podman exec -i -e HOME=/tmp/attic-home "nix-$user" sh -euc '
    IFS= read -r token
    umask 077
    mkdir -p "$HOME/.config/attic"
    printf "default-server = \"local\"\n[servers.local]\nendpoint = \"%s\"\ntoken = \"%s\"\n" \
      "$1" "$token" > "$HOME/.config/attic/config.toml"
    client=$2; shift 2
    exec "$client/bin/attic" "$@" </dev/null
  ' sh "$endpoint/" "$client" "$@" < "$state/admin.token"
}

case ${1:-status} in
  start) start ;;
  stop) podman stop -t 5 "$name" >/dev/null 2>&1 || true; echo "attic: stopped" ;;
  status)
    if [ "$(podman inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ]; then
      echo "attic: running at $endpoint/$cache"
      [ -s "$state/public-key" ] && echo "key: $(cat "$state/public-key")"
      client_run cache info "$cache"
    else
      echo "attic: not running (nix/attic/serve.sh start)" >&2; exit 1
    fi ;;
  env)
    echo "export ATTIC_ENDPOINT=$endpoint ATTIC_CACHE=$cache"
    printf "export NIX_EXTRA_CONF='extra-substituters = %s/%s\nextra-trusted-public-keys = %s'\n" \
      "$endpoint" "$cache" "$(cat "$state/public-key")" ;;
  client) shift; client_run "$@" ;;
  *) echo "usage: $0 start|stop|status|env|client <attic args>" >&2; exit 2 ;;
esac
