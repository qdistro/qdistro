# weston-qdistro — full upstream weston 16.0.0 with qdistro's functional
# libweston delta rebased in. Built by overriding nixpkgs' weston (itself
# 16.0.0, unpatched) so the frontend executable, shells and clients come
# back while the patched core keeps qdwin's required symbols.
#
# Carry list (nix/weston-delta.nix) = every modified file under libweston/ +
# include/libweston/ per nix/delta-audit.md; the `weston-delta-complete`
# check fails when the vendored tree and the list drift apart. EXCLUDING:
#   - top-level meson.build      (stripping glue; upstream keeps all subdirs)
#   - include/libweston/meson.build (its only hunk installs xwayland-api.h,
#     which upstream already installs via xwayland/meson.build — copying
#     would double-install)
{ lib, weston, vendoredSrc }:

let
  delta = import ./weston-delta.nix;
in

assert lib.assertMsg (weston.version == "16.0.0")
  "weston-qdistro copies whole files rebased on weston 16.0.0; nixpkgs now has ${weston.version} — re-audit nix/delta-audit.md and nix/weston-delta.nix";


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
