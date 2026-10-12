# qdistro-presentation — qdshell snapshot model / publisher / Qt adapter.
# Mirrors qdistro-presentation.spec: PyQt6 is a hard Requires there (the
# base package's qt extra), so it is a real dependency here, not optional.
{
  lib,
  python314Packages,
  qt6,
}:

python314Packages.buildPythonPackage {
  pname = "qdistro-presentation";
  version = "1.0.0";

  src = ../sdk/presentation;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  dependencies = [ python314Packages.pyqt6 ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [ qt6.qtbase ];

  nativeCheckInputs = [
    python314Packages.pytestCheckHook
    python314Packages.pytest-qt
  ];

  preCheck = ''
    export QT_QPA_PLATFORM=offscreen
    export HOME="$TMPDIR"
  '';

  meta = {
    description = "qdistro session presentation snapshot model, publisher, and Qt adapter";
    license = lib.licenses.gpl3Plus;
  };
}
