# Vendored QTermWidget Python SIP bindings source

This directory is a vendored copy of the `pyqt/` subdirectory from
[lxqt/qtermwidget](https://github.com/lxqt/qtermwidget) (version 2.4.0).

Only the SIP binding generator files are included — the actual C++
QTermWidget library must be installed via your distro
(`qtermwidget-devel` / `libqtermwidget6-3-dev` / `qtermwidget-qt6-devel`).

The `just build-sip` recipe uses these vendored files instead of cloning
the upstream repo, so:

- Builds work offline (after distro packages are installed)
- The SIP version is pinned to whatever was vendored
- Reproducible across installs

To upgrade: re-copy the `pyqt/` directory from upstream.

## License

These files are part of qtermwidget, licensed under
GPL-2.0-or-later (see https://github.com/lxqt/qtermwidget).
