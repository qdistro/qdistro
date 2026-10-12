# quickshell-vendored — builds qdshell/quickshell-vendored (upstream 0.3.2
# + vendored CLI11 2.7.2). Mirrors build-quickshell.sh: plain cmake with
# -DCMAKE_PREFIX_PATH pointing at the vendored CLI11Config.cmake shim
# (maps CLI11::CLI11 onto third-party/cli11). nixpkgs cli11 is 2.6.2 —
# we keep our vendored 2.7.2 for parity with the RPM artifact.
{
  lib,
  stdenv,
  cmake,
  ninja,
  pkg-config,
  qt6,
  spirv-tools,
  vulkan-headers,
  wayland,
  wayland-protocols,
  wayland-scanner,
  libdrm,
  libgbm,
  cpptrace,
  jemalloc,
  libxcb,
  pam,
  pipewire,
  glib,
  libsysprof-capture,
  polkit,
}:

stdenv.mkDerivation {
  pname = "qdistro-quickshell";
  version = "0.3.2";

  # Exclude only the wrapper's own outputs (src/build, src/build-*, *.lock)
  # at the vendored root — upstream legitimately ships src/src/build/,
  # */build.hpp.in and .github/workflows/build.yml, which a basename
  # filter would silently strip (same bug class as the RPM tarball fix).
  src =
    let
      root = ../qdshell/quickshell-vendored;
    in
    builtins.path {
      path = root;
      filter =
        path: type:
        let
          rel = lib.removePrefix "${toString root}/" (toString path);
        in
        !(rel == "src/build" || lib.hasPrefix "src/build-" rel || lib.hasSuffix ".lock" rel);
    };

  nativeBuildInputs = [
    cmake
    ninja
    pkg-config
    qt6.qtshadertools
    spirv-tools
    vulkan-headers
    wayland-scanner
    qt6.wrapQtAppsHook
  ];

  buildInputs = [
    qt6.qtbase
    qt6.qtdeclarative
    qt6.qtwayland
    qt6.qtsvg
    wayland
    wayland-protocols
    libdrm
    libgbm
    cpptrace
    jemalloc
    libxcb
    pam
    pipewire
    glib
    libsysprof-capture
    polkit
  ];

  dontUseCmakeConfigure = true;

  configurePhase = ''
    runHook preConfigure
    cmake -S src -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$out" \
      -DCMAKE_PREFIX_PATH="$PWD/cmake" \
      -DDISTRIBUTOR=qdistro
    runHook postConfigure
  '';

  buildPhase = ''
    runHook preBuild
    cmake --build build --parallel "$NIX_BUILD_CORES"
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    cmake --install build
    runHook postInstall
  '';

  meta = {
    description = "qdistro's vendored Quickshell 0.3.2";
    license = with lib.licenses; [ lgpl3Only bsd3 ];
  };
}
