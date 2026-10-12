# Validation gates as flake checks — run with `nix flake check`.
# Mirrors qdwin/libweston-vendored/run-production-symbols-test.sh plus the
# plugin-load smoke the investigation required, and launch smokes for the
# packaged GUI apps (each package's own unit suite runs in its checkPhase).
{
  lib,
  runCommand,
  runtimeShell,
  binutils,
  westonQdistro,
  libwestonVendored,
  qdwin,
  apps,
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

  # Launch the installed (wrapped) entry point and require it to still be
  # alive after the settle window — catches missing runtime deps, broken Qt
  # wrapping, and import-time crashes that unit suites (run from source)
  # cannot see.
  launchSmoke =
    name: exe: extraEnv:
    runCommand "${name}-launch" { } ''
      export HOME="$PWD/home" XDG_RUNTIME_DIR="$PWD/xdg"
      mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
      chmod 0700 "$XDG_RUNTIME_DIR"
      ${extraEnv}
      ${exe} > app.log 2>&1 &
      pid=$!
      sleep 10
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "${name} exited during the settle window:"; cat app.log; exit 1
      fi
      kill "$pid"; wait "$pid" 2>/dev/null || true
      cp app.log $out
    '';
  offscreen = "export QT_QPA_PLATFORM=offscreen";
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

  qfileman-launch = launchSmoke "qfileman" "${apps.qfileman}/bin/qfileman" offscreen;
  qnotebook-launch = launchSmoke "qnotebook" "${apps.qnotebook}/bin/qnotebook" offscreen;
  # The real QTermWidget binding spawns a PTY + shell on startup.
  qterminator-launch = launchSmoke "qterminator" "${apps.qterminator}/bin/qterminator" ''
    ${offscreen}
    export SHELL=${runtimeShell}
  '';
  # Chromium's own sandbox needs user namespaces the nix sandbox lacks.
  qdbrowser-launch = launchSmoke "qdbrowser" "${apps.qdbrowser}/bin/qdbrowser about:blank" ''
    ${offscreen}
    export QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"
  '';

  # End-to-end native stack: qfileman as a real Wayland client of the
  # nix-built weston-qdistro + qdwin (headless). Proves the compositor
  # accepts and maps a client's toplevel, not just that the plugin loads.
  qfileman-on-qdwin = runCommand "qfileman-on-qdwin" { } ''
    export HOME="$PWD/home" XDG_RUNTIME_DIR="$PWD/xdg"
    mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
    chmod 0700 "$XDG_RUNTIME_DIR"
    ${westonQdistro}/bin/weston --backend=headless --socket=wl-qd \
      --shell=${qdwin}/lib/weston/qdwin-shell.so --no-config > weston.log 2>&1 &
    wpid=$!
    for _ in $(seq 50); do [ -S "$XDG_RUNTIME_DIR/wl-qd" ] && break; sleep 0.2; done
    [ -S "$XDG_RUNTIME_DIR/wl-qd" ] || { cat weston.log; exit 1; }
    WAYLAND_DISPLAY=wl-qd QT_QPA_PLATFORM=wayland \
      ${apps.qfileman}/bin/qfileman > app.log 2>&1 &
    apid=$!
    sleep 10
    alive=0; kill -0 "$apid" 2>/dev/null && alive=1
    kill "$apid" 2>/dev/null || true; kill "$wpid" 2>/dev/null || true
    wait 2>/dev/null || true
    if [ "$alive" != 1 ]; then
      echo "--- app.log"; cat app.log; echo "--- weston.log"; cat weston.log; exit 1
    fi
    { echo "--- app.log"; cat app.log; echo "--- weston.log"; cat weston.log; } > $out
  '';
}
