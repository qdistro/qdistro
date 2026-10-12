# qfileman — PyQt6 file manager (leaf app).
# Mirrors qfileman.spec: PyQt6 + tomli-w + qdistro-presentation.
# Qt env wrapping is explicit: wrapQtAppsHook only wraps ELF/launchers
# it recognizes, so the installed bin/qfileman gets a wrapQtApp pass
# in postFixup regardless of hook heuristics.
{
  lib,
  python314Packages,
  qt6,
  dbus,
  rsync,
  git,
  gnutar,
  gzip,
  bzip2,
  xz,
  zstd,
  unzip,
  zip,
  p7zip,
  diffutils,
  glib,
  openssh,
  util-linux,
  xdg-utils,
  udisks,
  rclone,
  lftp,
  qdistro-presentation,
  qdistro-app-sdk,
  qtPytestCheck,
}:

python314Packages.buildPythonApplication {
  pname = "qfileman";
  version = "0.1.0";

  src = ../qdfileman;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  dependencies = [
    python314Packages.pyqt6
    python314Packages.tomli-w
    qdistro-presentation
    qdistro-app-sdk
  ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [
    qt6.qtbase
    qt6.qtwayland
  ];

  postFixup = ''
    wrapQtApp "$out/bin/qfileman"
  '';

  # The QtDBus paths (runner notifications, qterminator socket) block
  # without a bus — qtPytestCheck provides one, as qci's host container does.
  nativeCheckInputs = [
    dbus
    python314Packages.pytest
    python314Packages.pytest-qt
    # CLI tools the suite's plugin tests shell out to — qci's host image
    # has them, so tests assert instead of skip (rsync on PATH is what
    # keeps F5/F6 on the rsync runner path the cheat_aware tests cover).
    rsync
    git
    gnutar
    gzip
    bzip2
    xz
    zstd
    unzip
    zip
    p7zip
    diffutils
    glib
    openssh
    util-linux
    xdg-utils
    udisks
    rclone
    lftp
  ];

  checkPhase = qtPytestCheck { };

  meta.description = "qdistro file manager — dual-pane PyQt6 file manager with plugin system";
}
