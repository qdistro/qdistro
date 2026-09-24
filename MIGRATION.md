# Monorepo migration (2026-09-23)

Until 2026-09-23 qdistro was an umbrella repository, and its components lived
in separate repositories checked out as siblings. They now live in this
repository as top-level directories. Each component's full original `main`
history is kept, unrewritten, on a `legacy/multirepo/<name>` branch.

The migration is complete. On 2026-09-24 the eight component repositories in
the `qdistro` GitHub organization were renamed `legacy-<name>` and archived
(read-only; the old URLs redirect). `qterminator/qdterm` and
`qnotebook/qnotebook` were not renamed or archived. File issues against
https://github.com/qdistro/qdistro/issues.

| Directory | Original repository (historical) | Source commit | Legacy branch | Import commit | Repository now |
|---|---|---|---|---|---|
| `qdbrowser/` | https://github.com/qdistro/qdbrowser | `45e6edc23a1d` | `legacy/multirepo/qdbrowser` | `ab7983511e74` | [`qdistro/legacy-qdbrowser`](https://github.com/qdistro/legacy-qdbrowser) (archived) |
| `qdchrome-extension/` | https://github.com/qdistro/qdchrome-extension | `8f7cdf769858` | `legacy/multirepo/qdchrome-extension` | `e3bcdbef6e38` | [`qdistro/legacy-qdchrome-extension`](https://github.com/qdistro/legacy-qdchrome-extension) (archived) |
| `qdfileman/` | https://github.com/qdistro/qdfileman (local dir was `qfileman`) | `920aa45e8e7f` | `legacy/multirepo/qdfileman` | `314268d68a9d` | [`qdistro/legacy-qdfileman`](https://github.com/qdistro/legacy-qdfileman) (archived) |
| `qdfirefox-extension/` | https://github.com/qdistro/qdfirefox-extension | `d5aa682bdfd1` | `legacy/multirepo/qdfirefox-extension` | `6390fe71c23d` | [`qdistro/legacy-qdfirefox-extension`](https://github.com/qdistro/legacy-qdfirefox-extension) (archived) |
| `qdgreeter/` | https://github.com/qdistro/qdgreeter | `998b4abcba1c` | `legacy/multirepo/qdgreeter` | `6d85b404069f` | [`qdistro/legacy-qdgreeter`](https://github.com/qdistro/legacy-qdgreeter) (archived) |
| `qdlocker/` | https://github.com/qdistro/qdlocker | `2ee75a82143b` | `legacy/multirepo/qdlocker` | `02d58070b1fe` | [`qdistro/legacy-qdlocker`](https://github.com/qdistro/legacy-qdlocker) (archived) |
| `qdshell/` | https://github.com/qdistro/qdshell | `efd42d984db1` | `legacy/multirepo/qdshell` | `fb3f6cfd7e9f` | [`qdistro/legacy-qdshell`](https://github.com/qdistro/legacy-qdshell) (archived) |
| `qdterm/` | https://github.com/qterminator/qdterm (local dir was `qterminator`) | `9b02fe42a1e6` | `legacy/multirepo/qdterm` | `eebd0b5bca74` | `qterminator/qdterm` (not archived) |
| `qdwin/` | https://github.com/qdistro/qdwin | `2838b21ccc18` | `legacy/multirepo/qdwin` | `2deb4a2de0c9` | [`qdistro/legacy-qdwin`](https://github.com/qdistro/legacy-qdwin) (archived) |
| `qnotebook/` | https://github.com/qnotebook/qnotebook | `0b818d8c0150` | `legacy/multirepo/qnotebook` | `97d840cfe5ff` | `qnotebook/qnotebook` (not archived) |

Full SHAs and trees are in each import commit's trailers
(`Source-Commit:`, `Source-Tree:`), commit counts in its body. qdistro's own history continues unchanged
on `main`. The last pre-migration commit is
`d1d0aee8c3f88b73137b3b8ea266e13ab82174cd`.

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

    git bisect skip ab7983511e74^..4eb26dec45f1

Two harness tests stayed broken by the migration a little longer: the qdbrowser
VM bats (repo-root resolver, fixed in `6dd845279`) and `kiwi-ci-base.bats`
case 10 (log anchor, fixed in `2b775479a`). When bisecting with either of
them, also skip up to the fix:

    git bisect skip 4eb26dec45f1..2b775479a^

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
