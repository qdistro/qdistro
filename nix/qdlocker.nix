# qdlocker — PyQt6/QML screen locker over qdwin_locker_v1. Mirrors
# qdlocker.spec: PyQt6 + dbus-next + python-pam + pywayland (<0.5, the API
# the shipped scanner output targets) + QtQuick at runtime.
# Session unit lands in lib/systemd/user (systemd.packages convention) with
# ExecStart pointed at this store path; the PAM service file is shipped for
# the NixOS module to map onto security.pam.services (not active by itself).
{
  lib,
  python314Packages,
  python313,
  qt6,
  dbus,
  qdistro-presentation,
  qtPytestCheck,
}:

python314Packages.buildPythonApplication {
  pname = "qdlocker";
  version = (lib.importTOML ../qdlocker/pyproject.toml).project.version;

  src = ../qdlocker;

  pyproject = true;
  build-system = [ python314Packages.setuptools ];

  # qdistro-presentation: presentation.py imports it (guarded — without it
  # the locker silently keeps built-in defaults and ignores the managed
  # theme snapshot); undeclared in pyproject/spec, qci resolves it through
  # pytest's ../sdk/presentation pythonpath.
  dependencies = with python314Packages; [
    pyqt6
    dbus-next
    python-pam
    pywayland
    qdistro-presentation
  ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [
    qt6.qtbase
    qt6.qtdeclarative
    qt6.qtwayland
  ];

  postInstall = ''
    install -d "$out/lib/systemd/user"
    sed "s|ExecStart=/usr/local/bin/qdlocker|ExecStart=$out/bin/qdlocker|" \
      systemd/qdlocker.service > "$out/lib/systemd/user/qdlocker.service"
    grep -q "ExecStart=$out/bin/qdlocker" "$out/lib/systemd/user/qdlocker.service"
    install -Dm0644 pam/qdlocker "$out/etc/pam.d/qdlocker"
  '';

  postFixup = ''
    wrapQtApp "$out/bin/qdlocker"
  '';

  nativeCheckInputs = [
    dbus
    python314Packages.pytest
    python314Packages.pytest-qt
  ];

  # pyproject testpaths = tests/unit — the same selector qci's host gate uses.
  # test_logind_inhibitor.py's closed_fds fixture patches the process-global
  # os.close; on 3.14 asyncio's loop teardown closes real fds through it and
  # pollutes the record (product logic passes 30/30 with only the fake fds
  # recorded). Rather than drop that sleep-inhibitor coverage, run the file
  # on python313 — qci's host-image version — and the rest on python314.
  checkPhase = qtPytestCheck { args = "--ignore=tests/unit/test_logind_inhibitor.py"; };
  # env -u PYTHONPATH: the build env's 3.14 site-packages must not leak into
  # the 3.13 interpreter (qdlocker itself imports via pyproject pythonpath).
  postCheck = ''
    QD_PYTEST_TMP=$(mktemp -d "$TMPDIR/p3.XXXXXX")
    [ "''${#QD_PYTEST_TMP}" -le 40 ] || QD_PYTEST_TMP=$(mktemp -d /tmp/qdpt.XXXXXX)
    env -u PYTHONPATH ${python313.withPackages (ps: [
      ps.pytest
      ps.dbus-next
    ])}/bin/python -m pytest --basetemp="$QD_PYTEST_TMP/bt" tests/unit/test_logind_inhibitor.py
    rm -rf "$QD_PYTEST_TMP"
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    test -f "$out/${python314Packages.python.sitePackages}/qdlocker/qml/Main.qml"
    test -f "$out/${python314Packages.python.sitePackages}/qdlocker/qml/shim/qmldir"
  '';

  meta = {
    description = "qdistro screen locker — PyQt6/QML client of qdwin_locker_v1";
    license = lib.licenses.mit;
  };
}
