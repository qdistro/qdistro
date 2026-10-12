# weston-qdistro — full upstream weston 16.0.0 with qdistro's functional
# libweston delta rebased in. Built by overriding nixpkgs' weston (itself
# 16.0.0, unpatchced) so the frontend executable, shells and clients come
# back while the patched core keeps qdwin's required symbols.
#
# Carry list = every modified file under libweston/ + include/libweston/
# per nix/delta-audit.md, EXCLUDING:
#   - top-level meson.build      (stripping glue; upstream keeps all subdirs)
#   - include/libweston/meson.build (its only hunk installs xwayland-api.h,
#     which upstream already installs via xwayland/meson.build — copying
#     would double-install)
{ lib, weston, vendoredSrc }:

let
  # modified/added files to copy wholesale from the vendored tree
  delta = [
    "include/libweston/backend-rdp.h"
    "include/libweston/desktop.h"
    "libweston/backend-drm/drm-internal.h"
    "libweston/backend-drm/drm-kms-enums.h"
    "libweston/backend-drm/kms.c"
    "libweston/backend-headless/headless.c"
    "libweston/backend-rdp/rdp.c"
    "libweston/backend-rdp/rdp.h"
    "libweston/desktop/qdwin-xdg-constrain.h"
    "libweston/desktop/xdg-shell.c"
    "libweston/input.c"
    "libweston/output-capture.c"
    "libweston/output-capture.h"
    "libweston/pixman-renderer.c"
    "libweston/renderer-gl/gl-renderer.c"
  ];
in

# nixpkgs defaults pipewireSupport = false; qdistro's production libweston
# builds -Dbackend-pipewire=true (nix/libweston-vendored.nix) and qdwin links
# against THIS libweston, so the backend must ship in this closure.
(weston.override { pipewireSupport = true; }).overrideAttrs (old: {
  pname = "weston-qdistro";

  postPatch = ''
    ${lib.concatMapStringsSep "\n    " (f: "cp ${vendoredSrc}/${f} ${f}") delta}
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    for b in drm headless pipewire rdp wayland x11; do
      test -f $out/lib/libweston-16/$b-backend.so \
        || { echo "missing $b-backend.so"; exit 1; }
    done
  '';
})
