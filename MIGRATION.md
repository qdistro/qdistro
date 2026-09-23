<!-- REHEARSAL VALUES (2026-09-23, first rehearsal). At the freeze the sources
     are re-frozen and the imports re-created, so the Source-commit and
     Import-commit columns, the last pre-migration commit, the bisect range
     and the date MUST be re-filled from the execution log before this file
     is committed for real. -->
# Monorepo migration (2026-09-23)

Until 2026-09-23 qdistro was an umbrella repository, and its components lived
in separate repositories checked out as siblings. They now live in this
repository as top-level directories. Each component's full original `main`
history is kept, unrewritten, on a `legacy/multirepo/<name>` branch.

| Directory | Original repository | Source commit | Legacy branch | Import commit | Planned legacy repo |
|---|---|---|---|---|---|
| `qdbrowser/` | https://github.com/qdistro/qdbrowser | `997b4f64b04a` | `legacy/multirepo/qdbrowser` | `1c63fa963786` | `qdistro/legacy-qdbrowser` (not yet renamed) |
| `qdchrome-extension/` | https://github.com/qdistro/qdchrome-extension | `8f7cdf769858` | `legacy/multirepo/qdchrome-extension` | `6634fff52fea` | `qdistro/legacy-qdchrome-extension` (not yet renamed) |
| `qdfileman/` | https://github.com/qdistro/qdfileman (local dir was `qfileman`) | `920aa45e8e7f` | `legacy/multirepo/qdfileman` | `66357d771190` | `qdistro/legacy-qdfileman` (not yet renamed) |
| `qdfirefox-extension/` | https://github.com/qdistro/qdfirefox-extension | `d5aa682bdfd1` | `legacy/multirepo/qdfirefox-extension` | `20fd8f007060` | `qdistro/legacy-qdfirefox-extension` (not yet renamed) |
| `qdgreeter/` | https://github.com/qdistro/qdgreeter | `998b4abcba1c` | `legacy/multirepo/qdgreeter` | `0a8fe825b173` | `qdistro/legacy-qdgreeter` (not yet renamed) |
| `qdlocker/` | https://github.com/qdistro/qdlocker | `32e04c4d351c` | `legacy/multirepo/qdlocker` | `08b81c424b29` | `qdistro/legacy-qdlocker` (not yet renamed) |
| `qdshell/` | https://github.com/qdistro/qdshell | `efd42d984db1` | `legacy/multirepo/qdshell` | `6554bb1d2b57` | `qdistro/legacy-qdshell` (not yet renamed) |
| `qdterm/` | https://github.com/qterminator/qdterm (local dir was `qterminator`) | `9b02fe42a1e6` | `legacy/multirepo/qdterm` | `0f6347423289` | `qterminator/legacy-qdterm` (not yet renamed) |
| `qdwin/` | https://github.com/qdistro/qdwin | `65b29d9def71` | `legacy/multirepo/qdwin` | `77a14fc11888` | `qdistro/legacy-qdwin` (not yet renamed) |
| `qnotebook/` | https://github.com/qnotebook/qnotebook | `0b818d8c0150` | `legacy/multirepo/qnotebook` | `8047580bdfc1` | `qnotebook/legacy-qnotebook` (not yet renamed) |

Full SHAs, trees and commit counts are in each import commit's trailers
(`Source-Commit:`, `Source-Tree:`). qdistro's own history continues unchanged
on `main`. The last pre-migration commit is
`31ce4fe0248ee9d1088cebfb683f16463b3c5d54`.

`qdterm/` and `qdfileman/` take their GitHub repository names. Their Python
packages, binaries, desktop IDs and D-Bus names are still `qterminator` and
`qfileman`; renaming those is a separate product decision.

## Looking up old history

    git fetch origin 'refs/heads/legacy/multirepo/*:refs/remotes/origin/legacy/multirepo/*'
    git log origin/legacy/multirepo/qdwin -- qdwin/qdwin.c   # path WITHOUT the leading qdwin/

Legacy branches are separate histories. They are not merged into `main`, and
`git blame` on `main` stops at the import commit.

## Bisecting

The import commits and the layout repairs right after them add components
while the tree is still wired (wholly or partly) for the old sibling layout.
Mark that range `git bisect skip`:

    git bisect skip 1c63fa963786^..379d6e015

## What changed in the layout

- qci (`ci/bin/qci`) resolves components in-tree: `WORKSPACE` is the repo
  root. Host-gate rows `qfileman-pytest` / `qterminator-pytest` are now
  `qdfileman-pytest` / `qdterm-pytest`; `repo-state.tsv` has one row.
- Test VMs and the image get the whole repo as `/root/qdistro-src`
  (root content at the top, components beside it); the image manifest and
  `/etc/qdistro/release` carry one `SOURCE qdistro <sha>` line.
- The bootstrap clones one repository and the release source manifest pins
  one commit (`qdistro <sha>`).

## Notes

- qdshell is a fork of Noctalia shell. The fork base is
  `dbfe3634df0c57faf9772cecae1f2e92bd04de66` (upstream v4.5.0). Upstream
  tags were not imported.
- Licensing is per directory: see the License section of
  [README.md](README.md#license).
- Not migrated: the project tracker and `qdistro-site` (separate repositories).
- The per-component CI workflows were retired (GitHub runs workflows only from
  the root). `qdterm/.github/workflows/release.yml` and its issue templates are
  kept but inert, for a future monorepo release path.
