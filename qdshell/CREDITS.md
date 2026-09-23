# Credits

`qdshell` is a hard fork of
[Noctalia](https://github.com/noctalia-dev/noctalia-shell) v4.5.0
(commit `dbfe3634d`), tagged in this repository as
`fork-base/upstream-v4.5.0`. The full upstream history before the
fork is preserved with original hashes and commit dates — `git log
fork-base/upstream-v4.5.0` reaches it.

## Why fork

Noctalia upstream targets a multi-compositor desktop shell aimed at
hobbyist users. qdshell targets a single compositor (qdwin) with a
security-aware shell layer for qdistro. Every multi-compositor seam in
upstream is a place where qdistro's broker integration would have to
be re-stated, so the abstraction was removed.

## What we kept from upstream

The bulk of the codebase: `Modules/Bar/`, `Modules/Panels/`,
`Modules/Launcher/`, `Modules/LockScreen/`, `Modules/Notification/`,
`Modules/OSD/`, `Modules/Dock/`, `Modules/Cards/`, `Widgets/`,
`Commons/` (minus the upstream migration chain), most of `Services/`,
the plugin loader, the settings tab system, and the theming engine.

## Upstream contributors

- Noctalia maintainers: Lemmy, Ly-sec, and the full
  [contributor list](https://github.com/noctalia-dev/noctalia-shell/graphs/contributors).
- Quickshell framework:
  [outfoxxed and contributors](https://github.com/outfoxxed/quickshell).
- Tabler Icons (MIT) — see `Assets/Fonts/tabler/tabler-icons-license.txt`.

## License

Upstream Noctalia is MIT. MIT permits relicensing to GPL, and qdistro
is GPL-3.0-or-later across the board, so qdshell is relicensed to
match. Upstream contributors retain MIT rights on their original code.
See [LICENSE](LICENSE) for the qdshell license text.
