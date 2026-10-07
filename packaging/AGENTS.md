# packaging/ — RPM repository + Agama installer track

The repository-driven install path, alongside the kiwi raw-image pipeline in
`image/` (which has its own AGENTS.md and gates). Two subtrees:

- `rpm/` — 15 specs, deterministic source tarballs, podman builder
  Containerfile, signing/repo assembly, DUD spec.
- `agama/` — product definition, autoinstall profiles, OEMDRV/ISO tooling,
  test VM template.

## Rules

- **All external sources are configurable** through `env.sh` + optional
  gitignored `env.local.sh`. Never hardcode a URL, snapshot, mirror, or
  container tag inside a script/template — add a `@VAR@` placeholder and a
  default in `env.sh`.
- **No credentials in tracked files.** Rendered profiles in `agama/out/`
  embed `QDISTRO_*_PASSWORD` and are mode-600 + gitignored. The env.sh
  default passwords are test-only; say so where you surface them.
- **No binaries or secrets in git.** rpmbuild/ output, repos, ISOs, qcow2s,
  OEMDRV images, nvram, logs, and `keys/gnupg/` are all ignored; don't force-add.
- **Release-stamp grammar.** `/etc/qdistro/release` `SNAPSHOT=` requires 8
  digits when present (image-side readers parse it); for floating installs
  omit it and use `REPO_MODE=floating`. Don't stamp `PROFILE=release` unless
  the install satisfies the full bootstrap contract — use `dev`.
- **Install-time vs target repos are separate knobs** (`QDISTRO_TW_*_URL`
  vs `QDISTRO_TARGET_*_URL`). An install mirror must not silently become the
  system's update source.
- **Cloud/substrate images** for the VM lanes come from root `snapshot.conf`
  via `QDISTRO_TEST_SUBSTRATE=<manifest>` — reuse it; don't add parallel
  override code under `scripts/vm/` (that dir selects the heavy qci gates).
- **Bash conventions**: `set -euo pipefail`, `bash -n` clean, shellcheck
  clean at warning level. `*.in` templates are rendered by
  `agama/render-profile.sh`, which fails on leftover `@VAR@`.

## Validation

```sh
bash packaging/agama/render-profile.sh          # renders out/*.json, *.yaml, vm/*.xml
bash -n $(git ls-files 'packaging/*.sh')        # syntax
shellcheck --severity=warning packaging/ scripts/install/qdistro-session-provision.sh
```
