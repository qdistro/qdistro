# qdbrowser — PyQt6 + QtWebEngine browser. Mirrors qdbrowser.spec:
# PyQt6 + PyQt6-WebEngine + jeepney + qdistro-presentation (+ App1 SDK).
# Tests run one pytest process per file, as qci does — QtWebEngine residue
# accumulates across a single session (see qdbrowser/tests/AGENTS.md).
{
  lib,
  python314Packages,
  qt6,
  dbus,
  qdistro-presentation,
  qdistro-app-sdk,
  qtPytestCheck,
  nodejs,
}:

python314Packages.buildPythonApplication {
  pname = "qdbrowser";
  version = "0.1.0";

  src = ../qdbrowser;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  dependencies = with python314Packages; [
    pyqt6
    pyqt6-webengine
    jeepney
    # bin/qdbrowser-mcp imports mcp.server.fastmcp ([project.optional-
    # dependencies] mcp) — shipped command, so a hard dep here.
    mcp
    qdistro-presentation
    qdistro-app-sdk
  ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [
    qt6.qtbase
    qt6.qtwebengine
    qt6.qtwayland
  ];

  postFixup = ''
    for f in "$out"/bin/qdbrowser*; do
      wrapQtApp "$f"
    done
  '';

  nativeCheckInputs = [
    dbus
    python314Packages.pytest
    python314Packages.pytest-qt
    python314Packages.pillow
    # test-only, as in qci's host image: cert/CA-bundle tests importorskip
    # cryptography; reader-mode/restyle tests run their JS under node.
    python314Packages.cryptography
    nodejs
  ];

  checkPhase = qtPytestCheck { perFile = "tests/test_*.py"; };

  meta = {
    description = "qdistro first-party browser — PyQt6 + QtWebEngine";
    license = lib.licenses.gpl3Only;
  };
}
