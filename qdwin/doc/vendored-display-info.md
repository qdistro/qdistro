# Vendored display-info

`libweston-vendored/src/subprojects/display-info` is a nested upstream git
checkout of `libdisplay-info`, not a qdistro-owned source tree.

Current observed state:

- Upstream remote: `https://gitlab.freedesktop.org/emersion/libdisplay-info.git`
- Vendored version in `meson.build`: `0.1.1`
- A local `.meson-subproject-wrap-hash.txt` may exist in worktrees, but the
  nested repo does not carry qdistro provenance metadata.

Maintenance policy:

- Prefer the system `libdisplay-info` dependency when packaging allows it.
- If qdwin needs to vendor this code, record the upstream tag/commit, import
  date, local patches, and update procedure in the qdwin repo.
- Do not push qdistro vendoring notes to the upstream `display-info` remote.
- Avoid installing a vendored shared library as a system replacement unless the
  package explicitly intends to own that ABI.
