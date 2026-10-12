# Phase-0 validation gates as flake checks — run with `nix flake check`.
# Mirrors qdwin/libweston-vendored/run-production-symbols-test.sh plus the
# plugin-load smoke the investigation required.
{
  lib,
  runCommand,
  binutils,
  westonQdistro,
  libwestonVendored,
  qdwin,
}:

let
  popupHelpers = [
    "weston_desktop_xdg_popup_attach_layer_parent"
    "weston_desktop_xdg_popup_get_geometry"
    "weston_desktop_xdg_popup_set_layer_grab_handler"
    "weston_desktop_xdg_popup_dismiss_layer_grab"
  ];

  symbolCheck = name: lw:
    runCommand name { nativeBuildInputs = [ binutils ]; } ''
      for s in ${lib.concatStringsSep " " popupHelpers}; do
        nm -D --defined-only ${lw}/lib/libweston-16.so.0 | grep -q " $s\$" \
          || { echo "MISSING exported symbol: $s"; exit 1; }
        echo "OK  $s"
      done
      touch $out
    '';
in

{
  # The production symbol gate on both libweston flavors.
  libweston-vendored-symbols = symbolCheck "libweston-vendored-symbols" libwestonVendored;
  weston-qdistro-symbols = symbolCheck "weston-qdistro-symbols" westonQdistro;

  # The plugin-load check: headless weston-qdistro dlopens qdwin-shell.so —
  # proves the executable resolves the deliberately-undefined shell symbols
  # (screenshooter_create, wet_shell_init) and that the layer-popup grab
  # handler registers.
  qdwin-plugin-load = runCommand "qdwin-plugin-load" { } ''
    export XDG_RUNTIME_DIR="$PWD/xdg"
    mkdir -p "$XDG_RUNTIME_DIR"
    timeout 15 ${westonQdistro}/bin/weston \
      --backend=headless \
      --shell=${qdwin}/lib/weston/qdwin-shell.so \
      --no-config > weston.log 2>&1 || true
    grep -q "qdwin: shell loaded" weston.log \
      || { cat weston.log; exit 1; }
    grep -q "layer-popup grab handler registered" weston.log \
      || { cat weston.log; exit 1; }
    cp weston.log $out
  '';
}
