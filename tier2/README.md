# Tier-2 — rootless podman + nested compositor

Spec ref: `doc/isolation-tiers.md` tier 2; design landing page
`doc/containers.md`.

## Architecture

```
admin compositor (qdwin) ← outer
 │
 ▼ wayland-1 (UNIX socket, /run/user/1000/wayland-1)
 │
[qdistro-secctx-exec]    ← wraps the spawn with wp_security_context_v1
 │                         (sandbox_engine="qdistro.tier2",
 │                          app_id="<container>/<app>",
 │                          instance_id=<launch-token>)
 ▼
[podman run --userns=keep-id -v /run/user/1000:/run/user/1000 ...]
 │
 ▼ inside the container
 │
[weston --shell=qdwin-shell.so --backend=wayland-backend.so,pipewire-backend.so]
 │       (env QDWIN_NESTED_MODE=1, QDWIN_OUTER_DISPLAY=wayland-secctx-NN)
 │
 │  inner weston binds qdwin_nested_manager_v1 on the outer; for each
 │  inner xdg_toplevel it captures the surface to a PipeWire output
 │  and calls advertise_toplevel(pw_node, input_sink, app_id, title,
 │  origin_uid). Outer qdwin creates a proxy surface that qdshell
 │  decorates exactly like any other toplevel.
 │
 ▼
[guest app: weston-terminal, firefox, ...]
```

One **chromed peer toplevel per inner app** — every guest window is a
regular `xdg_toplevel` on the outer compositor, indistinguishable in
the shell from a tier-0 native app. Stacks, focuses, and gets the
broker clipboard/handoff gates like every other toplevel.

## Files

- `spawn-tier2.sh` — host wrapper. Allocates a launch token, wraps
  the podman invocation in `qdistro-secctx-exec`, runs the container
  rootless with `--userns=keep-id`. Emits the launch token on stdout
  so qdshell can correlate the eventual `toplevel_added`.
- `make-tier2-image.sh` — produces `qdistro/tier2-<workload>:latest`
  podman images, one per workload. First workload:
  `qdistro/tier2-weston-terminal:latest` (the bats minimum).
- `Containerfile.weston-terminal` — minimal openSUSE Tumbleweed base +
  weston (with backend-pipewire + wayland-backend) + qdwin-shell.so +
  weston-terminal + the in-container entrypoint.
- `entrypoint.sh` — runs inside the container. Starts inner weston in
  the foreground with the qdwin-shell.so plugin, exec's the guest app
  once the inner socket appears.
- `podapps-scan.sh` — host helper that uses `podman exec` to enumerate
  `.desktop` files inside a container, writes them as the host-side
  cache for the qdshell launcher (see Phase B in `doc/containers.md`).

## Image-per-workload model

Each workload ships its own image:

| Image                            | Workload      |
|----------------------------------|---------------|
| `qdistro/tier2-weston-terminal`  | weston-terminal (bats minimum) |
| `qdistro/tier2-text-viewer`      | text-viewer (open class `text/plain`, network none) |
| `qdistro/tier2-url-preview`      | url-preview (open class `url-preview-known-origin`, network egress) |
| `qdistro/tier2-firefox`          | firefox       |
| `qdistro/tier2-libreoffice`      | libreoffice   |

### text-viewer / url-preview workload images

Two open-in-disposable workloads ship their own image + a deny-by-default
seccomp profile (`tier2/seccomp/<workload>.json`). The workload binaries live
in `tier2/workload/`:

- **text-viewer** (`text/plain` class, network **none**): pages the single
  read-only file mounted at `/mnt/input/<basename>` in a weston-terminal running
  `less` in a NON-raw mode (control bytes shown as caret notation, never
  executed). Fails closed unless exactly one regular file is present; never
  recurses; the filename is passed argv-only (no shell interpolation). Reuses the
  proven weston-terminal binary — no GUI toolkit.
- **url-preview** (`url-preview-known-origin` class, network **egress**): a
  bounded NETWORK METADATA / TEXT preview (NOT a browser / visual renderer). It
  reads ONE URL from the read-only `/mnt/input` file, validates it strictly
  (http/https only, single line, length-capped, no creds, no control bytes/
  whitespace), fetches bounded response headers + a capped body with `curl`
  (connect/total timeouts, `--max-filesize`, **no redirect following**, `-q` so
  no curlrc), escapes all control bytes (`cat -v`), and pages the sanitized
  result. "Known-origin" is a POLICY claim enforced upstream by the
  broker-authorized caller + the `qdistro.dispose.open:` gate; the container
  enforces URL SHAPE + bounded fetches only (an origin allowlist is residual).
  A rich visual renderer (headless chromium / webkit) is an explicit residual.

Rationale: smaller images, faster start, no shared mutable state
between workloads, simpler attack surface per image. The qdshell
launcher merges every container's apps into one badged list; the
user does not see the image boundary.

## Run (manual)

```bash
# Build the weston-terminal image:
bash tier2/make-tier2-image.sh weston-terminal

# Spawn the workload:
tier2/spawn-tier2.sh tier2-c1 weston-terminal -- weston-terminal

# Stop:
podman stop tier2-c1
```

## Tests

`tests/integration/vm/tiered-isolation.bats` covers tier-2 end-to-end
(s32 podman, s33 input, s34 lifecycle). See `doc/containers.md` for
the cold-start UX contract that the qdshell-side tests exercise.
