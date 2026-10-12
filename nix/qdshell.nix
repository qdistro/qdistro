# qdshell — Qdistro.Qdwin QML plugin (meson) + the QML tree.
# Mirrors qdshell.spec: meson installs libqdistro-qdwin.so + qmldir to
# share/qdistro/qml/Qdistro/Qdwin/; the QML tree is copied verbatim to
# share/quickshell/qdshell/ minus dev-only content.
# Needs qdistro-protocols.pc from the qdwin package on PKG_CONFIG_PATH.
{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  qt6,
  wayland,
  wayland-scanner,
  wayland-protocols,
  makeWrapper,
  qdwin,
  quickshell,
}:

let
  qdshellSrc = lib.fileset.toSource {
    root = ../qdshell;
    fileset = lib.fileset.unions (
      [ ../qdshell/meson.build ../qdshell/qml-plugin ]
      ++ map (d: ../qdshell + "/${d}") qmlDirs
      ++ map (f: ../qdshell + "/${f}") qmlFiles
    );
  };
  qmlDirs = [
    "Assets"
    "Commons"
    "Helpers"
    "Modules"
    "Scripts"
    "Services"
    "Shaders"
    "Widgets"
  ];
  qmlFiles = [
    "shell.qml"
    "LICENSE"
    "README.md"
    "CREDITS.md"
  ];
in

stdenv.mkDerivation {
  pname = "qdshell";
  version = "0.1.0";

  # Explicit fileset — the session payload + plugin build only.
  # quickshell-vendored/ is its own derivation; tests/Tests/scripts/nix/
  # node_modules/flake files are dev-only per qdshell.spec.
  src = qdshellSrc;

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    wayland-scanner
    makeWrapper
    qt6.wrapQtAppsHook
  ];

  buildInputs = [
    qt6.qtbase
    qt6.qtdeclarative
    wayland
    wayland-protocols
    qdwin
  ];

  postInstall = ''
    mkdir -p $out/share/quickshell/qdshell
    ${lib.concatMapStringsSep "\n    " (d: "cp -r ${qdshellSrc}/${d} $out/share/quickshell/qdshell/") qmlDirs}
    ${lib.concatMapStringsSep "\n    " (f: "cp ${qdshellSrc}/${f} $out/share/quickshell/qdshell/") qmlFiles}
    chmod -R u=rwX,go=rX $out/share/quickshell/qdshell

    # The session launcher: deploy/qdshell.service runs
    #   qs -p /usr/share/quickshell/qdshell
    # with QML_IMPORT_PATH=/usr/share/qdistro/qml (so `import Qdistro.Qdwin`
    # resolves) and QS_ICON_THEME=Adwaita. Same contract, store paths.
    makeWrapper ${quickshell}/bin/qs $out/bin/qdshell \
      --add-flags "-p $out/share/quickshell/qdshell" \
      --prefix QML_IMPORT_PATH : $out/share/qdistro/qml \
      --set-default QS_ICON_THEME Adwaita
  '';

  # Mirror the spec's %check.
  doInstallCheck = true;
  installCheckPhase = ''
    test -f $out/share/qdistro/qml/Qdistro/Qdwin/libqdistro-qdwin.so
    test -f $out/share/qdistro/qml/Qdistro/Qdwin/qmldir
    test -f $out/share/quickshell/qdshell/shell.qml
    test -x $out/bin/qdshell
  '';

  meta = {
    description = "qdistro desktop shell — Qdistro.Qdwin QML plugin + QML tree";
    license = lib.licenses.gpl3Plus;
  };
}
