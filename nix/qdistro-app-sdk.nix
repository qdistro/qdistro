# qdistro_app — the App1 receiver/registration SDK package.
# Loose package dir (no pyproject); install-sdk-for-vm.sh drops it into
# site-packages on the RPM side — same thing here. Needs dbus-python
# (with its GLib mainloop bindings) for the receiver path.
{
  lib,
  python314Packages,
}:

python314Packages.buildPythonPackage {
  pname = "qdistro-app-sdk";
  version = "0.1.0";

  src = ../sdk/qdistro_app;

  format = "other";

  dependencies = [ python314Packages.dbus-python ];

  installPhase = ''
    runHook preInstall
    site="$out/${python314Packages.python.sitePackages}"
    mkdir -p "$site/qdistro_app"
    cp ./*.py "$site/qdistro_app/"
    runHook postInstall
  '';

  pythonImportsCheck = [ "qdistro_app" ];

  meta = {
    description = "qdistro App1 SDK — app receiver, registration helper, recall module";
    license = lib.licenses.gpl3Plus;
  };
}
