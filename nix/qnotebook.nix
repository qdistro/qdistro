# qnotebook — PyQt6 personal wiki (leaf app). Mirrors qnotebook.spec:
# PyQt6 + mistune + qdistro-presentation (+ the App1 SDK it imports).
{
  lib,
  python314Packages,
  qt6,
  dbus,
  git,
  qdistro-presentation,
  qdistro-app-sdk,
  qtPytestCheck,
}:

python314Packages.buildPythonApplication {
  pname = "qnotebook";
  version = "0.0.1";

  src = ../qnotebook;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  dependencies = [
    python314Packages.pyqt6
    python314Packages.mistune
    qdistro-presentation
    qdistro-app-sdk
  ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [
    qt6.qtbase
    qt6.qtwayland
  ];

  postFixup = ''
    wrapQtApp "$out/bin/qnotebook"
  '';

  # git: versioning + merge-ladder (git merge-file) tests; optional at
  # runtime (shutil.which), present in qci's host image.
  nativeCheckInputs = [
    dbus
    git
    python314Packages.pytest
    python314Packages.pytest-qt
  ];

  checkPhase = qtPytestCheck { };

  meta = {
    description = "qdistro notes/wiki app — PyQt6 Zim-style wiki editor";
    license = lib.licenses.gpl2Plus;
  };
}
