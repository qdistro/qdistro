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

  # qci runs the host suite inside dbus-run-session; the QtDBus paths
  # (runner notifications, qterminator socket) block without a bus.
  # dbus-run-session needs /etc/dbus-1 — spawn dbus-daemon against the
  # package's own session.conf instead, and mirror enter-host.sh's
  # system-bus alias.
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

  checkPhase = ''
    runHook preCheck
    export QT_QPA_PLATFORM=offscreen
    export HOME="$TMPDIR"
    export DBUS_SESSION_BUS_ADDRESS="$(dbus-daemon --fork --print-address=1 --config-file=${dbus}/share/dbus-1/session.conf)"
    export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    # tmp_path under /nix/var/nix/builds/<hash>/ exceeds the 108-byte
    # AF_UNIX limit — qterminator socket tests need a short basetemp.
    python -m pytest --basetemp=/tmp/qfm-pytest
    runHook postCheck
  '';

  meta.description = "qdistro file manager — dual-pane PyQt6 file manager with plugin system";
}
