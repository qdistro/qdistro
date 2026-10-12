# qdgreeter — PyQt6/QML greetd greeter. Mirrors qdgreeter.spec: PyQt6 +
# QtQuick at runtime. The debug unit goes to lib/systemd/user with its
# ExecStart pointed at this store path (production runs it from greetd's
# default_session, which the NixOS module owns).
{
  lib,
  python314Packages,
  qt6,
  dbus,
  qtPytestCheck,
}:

python314Packages.buildPythonApplication {
  pname = "qdgreeter";
  version = "0.1.0";

  src = ../qdgreeter;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  dependencies = [ python314Packages.pyqt6 ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [
    qt6.qtbase
    qt6.qtdeclarative
    qt6.qtwayland
  ];

  postInstall = ''
    install -d "$out/lib/systemd/user"
    sed "s|ExecStart=/usr/bin/qdgreeter|ExecStart=$out/bin/qdgreeter|" \
      systemd/qdgreeter.service > "$out/lib/systemd/user/qdgreeter.service"
    grep -q "ExecStart=$out/bin/qdgreeter" "$out/lib/systemd/user/qdgreeter.service"
  '';

  postFixup = ''
    wrapQtApp "$out/bin/qdgreeter"
  '';

  nativeCheckInputs = [
    dbus
    python314Packages.pytest
    python314Packages.pytest-qt
  ];

  # qci: `python3 -m pytest tests`.
  checkPhase = qtPytestCheck { args = "tests"; };

  doInstallCheck = true;
  installCheckPhase = ''
    test -f "$out/${python314Packages.python.sitePackages}/qdgreeter/qml/Main.qml"
  '';

  meta.description = "qdistro boot greeter — PyQt6/QML greetd greeter";
}
