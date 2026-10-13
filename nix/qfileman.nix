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

let
  # Tools the shipped plugins/actions shell out to (F5/F6 + rsync sync,
  # archive pack/unpack, checksum/diff, git status, gio trash/mounts,
  # lsblk/udisksctl mount manager, sftp/rclone/lftp remote copy, xdg-open).
  # On the wrapper's PATH (suffix — a host tool still wins) so a clean
  # install has every capability its tests prove.
  runtimeTools = [
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
  # The real disposable-class resolver + registry from the monorepo root, so
  # test_class_enabled_against_real_resolver runs instead of skipping (it
  # walks up for <repo>/session_manager/, absent from the component src).
  sessionManager = lib.fileset.toSource {
    root = ../session_manager;
    fileset = lib.fileset.unions [
      ../session_manager/qdistro_disposable_classes.py
      ../session_manager/disposable-classes.toml
      ../session_manager/qdistro_disposables.py
    ];
  };
in

python314Packages.buildPythonApplication {
  pname = "qfileman";
  version = (lib.importTOML ../qdfileman/pyproject.toml).project.version;

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

  makeWrapperArgs = [ "--suffix" "PATH" ":" (lib.makeBinPath runtimeTools) ];

  postFixup = ''
    wrapQtApp "$out/bin/qfileman"
  '';

  # The QtDBus paths (runner notifications, qterminator socket) block
  # without a bus — qtPytestCheck provides one, as qci's host container does.
  nativeCheckInputs = [
    dbus
    python314Packages.pytest
    python314Packages.pytest-qt
    # Same tools on the test PATH: qci's host image has them, so plugin
    # tests assert instead of skip (rsync is what keeps F5/F6 on the rsync
    # runner path the cheat_aware tests cover).
  ] ++ runtimeTools;

  # Lay it out as a sibling of the source dir, matching the monorepo shape
  # the test's walk-up expects (an env override would leak into every test).
  preCheck = ''
    cp -r ${sessionManager} "$NIX_BUILD_TOP/session_manager"
  '';
  checkPhase = qtPytestCheck { };

  meta = {
    description = "qdistro file manager — dual-pane PyQt6 file manager with plugin system";
    license = lib.licenses.gpl3Only;
  };
}
