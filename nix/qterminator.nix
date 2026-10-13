# qterminator — PyQt6 + QTermWidget terminal emulator (leaf app).
# Mirrors qterminator.spec: PyQt6 + qdistro-presentation. The QTermWidget
# SIP binding is a real runtime dep (conftest fakes it when absent — the
# package gets the genuine binding, not the stub).
{
  lib,
  python314Packages,
  qt6,
  dbus,
  qdistro-presentation,
  qdistro-app-sdk,
  qtermwidget-py,
  qtPytestCheck,
}:

python314Packages.buildPythonApplication {
  pname = "qterminator";
  version = (lib.importTOML ../qdterm/pyproject.toml).project.version;

  src = ../qdterm;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  dependencies = [
    python314Packages.pyqt6
    # tmux_web_share plugin's transport signing; undeclared in pyproject
    # but imported by the module (and its test) — a real runtime dep.
    python314Packages.cryptography
    # bin/qterminator-mcp imports mcp.server.fastmcp; shadow_screen and the
    # asciinema/instant-replay/agent-control plugins import pyte. Neither is
    # declared in pyproject; both are needed by shipped features.
    python314Packages.mcp
    python314Packages.pyte
    qdistro-presentation
    qdistro-app-sdk
    qtermwidget-py
  ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [
    qt6.qtbase
    qt6.qtwayland
  ];

  # All three console entry points need Qt env.
  postFixup = ''
    for f in "$out"/bin/qterminator*; do
      wrapQtApp "$f"
    done
  '';

  nativeCheckInputs = [
    dbus
    python314Packages.pytest
    python314Packages.pytest-qt
  ];

  # --ignore mirrors qci's host gate (ci/lib/gates/host.sh qdterm-pytest),
  # which excludes test_print_terminal.py as printer-coupled. Independently
  # of printers, its two print-buffer tests hang here: their FakeInnerTerm
  # lacks screenColumnsCount/screenLinesCount, so pdf_export._buffer_text
  # reads "" and the plugin opens a modal QMessageBox.
  # test_geometry_negative_values_rejected pins a Python 3.13 argparse quirk
  # (qci's host image is 3.13.15); 3.14 argparse accepts "-100x-200" as the
  # option value, so on the shipping python314 it does not raise.
  checkPhase = qtPytestCheck {
    args = lib.concatStringsSep " " [
      "--ignore=tests/test_print_terminal.py"
      "--deselect=tests/test_cli.py::test_geometry_negative_values_rejected"
    ];
  };

  meta = {
    description = "qdistro terminal emulator — PyQt6 Terminator port on QTermWidget";
    license = lib.licenses.gpl3Only;
  };
}
