# qdwin — the compositor shell plugin (qdwin-shell.so) + qdistro-protocols.
# Builds against weston-qdistro's libweston-16 (patched delta). Symbols like
# screenshooter_create stay undefined by design — resolved at runtime from
# the weston executable (--allow-shlib-undefined in qdwin's meson).
{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  python3,
  westonQdistro,
  wayland,
  wayland-scanner,
  wayland-protocols,
  libinput,
  libxcursor,
  libX11,
  xorgproto,
  pixman,
  libxkbcommon,
}:

stdenv.mkDerivation {
  pname = "qdwin";
  version = import ./meson-version.nix ../qdwin/meson.build;

  src = builtins.path {
    path = ../qdwin;
    filter =
      path: type:
      !(lib.hasPrefix "build" (baseNameOf path) || lib.hasSuffix ".lock" path);
  };

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    python3
    wayland-scanner
    wayland-protocols
  ];

  buildInputs = [
    westonQdistro
    wayland
    libinput
    libxcursor
    libX11
    xorgproto
    pixman
    libxkbcommon
  ];

  meta = {
    description = "qdistro compositor shell plugin for weston (libweston-16)";
    license = lib.licenses.gpl3Plus;
  };
}
