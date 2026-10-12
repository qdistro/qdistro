# libweston-vendored — builds qdwin/libweston-vendored/src/ directly.
# The vendored tree is already patched in place (see delta-audit.md);
# its own meson.build builds libweston + backend modules only.
# Flags mirror build-libweston.sh's "production" profile.
{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  python3,
  wayland,
  wayland-protocols,
  wayland-scanner,
  pixman,
  libxkbcommon,
  libinput,
  libevdev,
  libdrm,
  libgbm,
  libGL,
  seatd,
  libdisplay-info_0_3,
  lcms2,
  cairo,
  pango,
  libjpeg,
  libwebp,
  freerdp,
  pipewire,
  xwayland,
  libxcursor,
  libxcb-cursor,
  xcbutilwm,
}:

stdenv.mkDerivation {
  pname = "qdistro-libweston-vendored";
  version = "16.0.0";

  src = builtins.path {
    path = ../qdwin/libweston-vendored/src;
    filter = path: type:
      !(lib.hasPrefix "build-" (baseNameOf path) || lib.hasSuffix ".lock" path);
  };

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    python3
    wayland-scanner
  ];

  buildInputs = [
    wayland
    wayland-protocols
    pixman
    libxkbcommon
    libinput
    libevdev
    libdrm
    libgbm
    libGL
    seatd
    libdisplay-info_0_3
    lcms2
    cairo
    pango
    libjpeg
    libwebp
    freerdp
    pipewire
    xwayland
    libxcursor
    libxcb-cursor
    xcbutilwm
  ];

  # nix's mesonConfigurePhase already injects -Dwrap_mode=nodownload.
  mesonFlags = [
    "-Dbackend-drm=true"
    "-Dbackend-rdp=true"
    "-Dbackend-vnc=false"
    "-Dbackend-pipewire=true"
    "-Dbackend-wayland=true"
    "-Dbackend-x11=true"
    "-Dbackend-headless=true"
    "-Dbackend-default=drm"
    "-Drenderer-gl=true"
    "-Drenderer-vulkan=false"
    "-Dxwayland=true"
    "-Dcolor-management-lcms=true"
  ];

  meta.description = "qdistro's patched libweston-16 + backend modules";
}
