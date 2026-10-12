# qdistro-daemons — C helpers driven by qdwin's qdistro-protocols.
# Mirrors qdistro-daemons.spec: wayland-client + scanner + qdistro-protocols;
# freerdp/pipewire/selinux paths are meson `required: false` but we provide
# all of them so nothing silently skips.
{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  wayland,
  wayland-scanner,
  wayland-protocols,
  qdwin,
  freerdp,
  pipewire,
  libselinux,
  libXcursor,
  libX11,
  xorgproto,
}:

stdenv.mkDerivation {
  pname = "qdistro-daemons";
  version = "0.1.0";

  src = ../daemons;

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    wayland-scanner
    wayland-protocols
  ];

  buildInputs = [
    wayland
    qdwin
    freerdp
    pipewire
    libselinux
    libXcursor
    libX11
    xorgproto
  ];

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    meson test --print-errorlogs
    runHook postCheck
  '';

  meta.description = "qdistro wayland helper daemons (forward, pixelfeed, secctx-exec, tier1-exec, cursor-sprites)";
}
