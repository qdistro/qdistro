# QTermWidget Python binding — SIP wheel built from qdterm/qtermwidget-pyqt.
# Mirrors util/build-sip.sh: inject PyQt6's sip bindings dir into
# [tool.sip.project] and the qtermwidget6 header/lib paths into project.py,
# then `python -m sipbuild.tools.wheel` produces the abi3 wheel.
{
  lib,
  stdenv,
  python314Packages,
  qt6,
  lxqt,
  autoPatchelfHook,
}:

let
  py = python314Packages;
  buildPy = py.python.withPackages (ps: [
    ps.sip
    ps.pyqt-builder
    ps.pyqt6
    ps.installer
  ]);
in

stdenv.mkDerivation {
  pname = "python3.14-qtermwidget";
  version = "2.4.0";

  src = ../qdterm/qtermwidget-pyqt;

  nativeBuildInputs = [
    buildPy
    autoPatchelfHook
  ];

  buildInputs = [
    lxqt.qtermwidget
    qt6.qtbase
    buildPy
  ];

  # Library, not an app — autoPatchelf handles the .so's deps.
  dontWrapQtApps = true;

  buildPhase = ''
    runHook preBuild
    # Same two injections build-sip.sh does: PyQt6 .sip search path and the
    # qtermwidget6 header/library locations.
    printf '\n[tool.sip.project]\nsip-include-dirs = ["%s"]\n' \
      "${py.pyqt6}/${py.python.sitePackages}/PyQt6/bindings" >> pyproject.toml
    sed -i "s|self.libraries.append('qtermwidget6')|self.libraries.append('qtermwidget6')\n        self.include_dirs.append('${lxqt.qtermwidget}/include/qtermwidget6')\n        self.library_dirs.append('${lxqt.qtermwidget}/lib')|" project.py
    python -m sipbuild.tools.wheel --qmake ${qt6.qtbase}/bin/qmake6
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    # sip-wheel emits the wheel in cwd with a case-varying name
    # (qtermwidget-* here — same nocaseglob guard as build-sip.sh).
    shopt -s nullglob nocaseglob
    wheels=( qtermwidget-*.whl )
    if [ ''${#wheels[@]} -eq 0 ]; then
      echo "no QTermWidget wheel produced" >&2; exit 1
    fi
    python -m installer --prefix "$out" "''${wheels[0]}"
    runHook postInstall
  '';

  # autoPatchelfHook binds the extension .so against qtermwidget6 + Qt.

  meta.description = "QTermWidget 2.4.0 SIP Python binding (built from qdterm's vendored pyqt source)";
}
