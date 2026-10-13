# Validation gates as flake checks — run with `nix flake check`.
# Mirrors qdwin/libweston-vendored/run-production-symbols-test.sh, the
# plugin-load + layer-popup protocol checks the investigation required,
# launch smokes for the packaged GUI apps, and end-to-end runs on the
# nix-built compositor. (Each package's own unit suite runs in its
# checkPhase; flake.nix also puts every package into `checks`.)
{
  lib,
  runCommand,
  runtimeShell,
  binutils,
  coreutils,
  dbus,
  pkg-config,
  python314,
  wayland-scanner,
  wayland-protocols,
  westonQdistro,
  westonUpstreamSrc,
  vendoredSrc,
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
      # nm to a file first: `nm | grep -q` under pipefail fails whenever grep
      # exits early and nm takes SIGPIPE, reporting a present symbol missing.
      nm -D --defined-only ${lw}/lib/libweston-16.so.0 > syms.txt
      for s in ${lib.concatStringsSep " " popupHelpers}; do
        grep -q " $s\$" syms.txt \
          || { echo "MISSING exported symbol: $s"; exit 1; }
        echo "OK  $s"
      done
      touch $out
    '';

  # Shell helpers for checks that run a headless weston-qdistro + qdwin.
  # The compositor must still be alive at every assertion point, and must
  # stop on our SIGTERM (exit 0 or 143) — a crash is never a pass.
  westonLib = ''
    export HOME="$PWD/home" XDG_RUNTIME_DIR="$PWD/xdg"
    mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
    chmod 0700 "$XDG_RUNTIME_DIR"
    dump() { for f in "$@"; do echo "--- $f"; cat "$f"; done; }
    die() { echo "FAIL: $1"; shift; dump "$@"; exit 1; }
    start_weston() {  # $1 = socket name; extra args follow
      local sock=$1; shift
      ${westonQdistro}/bin/weston --backend=headless --socket="$sock" \
        --shell=${qdwin}/lib/weston/qdwin-shell.so --no-config "$@" \
        > weston.log 2>&1 &
      wpid=$!
      for _ in $(seq 75); do
        [ -S "$XDG_RUNTIME_DIR/$sock" ] && return 0
        kill -0 "$wpid" 2>/dev/null || die "weston exited during startup" weston.log
        sleep 0.2
      done
      die "weston socket $sock never appeared" weston.log
    }
    weston_alive() { kill -0 "$wpid" 2>/dev/null || die "weston died ($1)" weston.log "''${@:2}"; }
    wait_log() {  # $1 = regex, $2 = timeout s, rest = logs to dump on failure
      local re=$1 t=$2; shift 2
      for _ in $(seq $((t * 5))); do
        grep -qE "$re" weston.log && return 0
        weston_alive "waiting for /$re/" "$@"
        sleep 0.2
      done
      die "no /$re/ in weston.log within ''${t}s" weston.log "$@"
    }
    stop_weston() {
      weston_alive "before shutdown" "$@"
      kill -TERM "$wpid"; local rc=0; wait "$wpid" || rc=$?
      case $rc in 0|143) ;; *) die "weston exit status $rc on SIGTERM" weston.log "$@" ;; esac
    }
    start_bus() {
      dbus-daemon --fork --print-address=3 --print-pid=4 \
        --config-file=${dbus}/share/dbus-1/session.conf 3>bus.addr 4>bus.pid
      export DBUS_SESSION_BUS_ADDRESS="$(cat bus.addr)"
    }
  '';
  westonCheck = name: script:
    runCommand name { nativeBuildInputs = [ dbus pkg-config wayland-scanner ]; } (westonLib + script);

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

  pyWayland = python314.withPackages (ps: [ ps.pywayland ]);
  qdwinXml = "${qdwin}/share/qdistro/protocols";
in

{
  # The production symbol gate on both libweston flavors.
  libweston-vendored-symbols = symbolCheck "libweston-vendored-symbols" libwestonVendored;
  weston-qdistro-symbols = symbolCheck "weston-qdistro-symbols" westonQdistro;

  # Headless weston-qdistro dlopens qdwin-shell.so — proves the executable
  # resolves the deliberately-undefined shell symbols (screenshooter_create,
  # wet_shell_init) and the layer-popup grab handler registers; the
  # compositor must still be up afterwards and shut down cleanly.
  qdwin-plugin-load = westonCheck "qdwin-plugin-load" ''
    start_weston wl-qd
    wait_log "qdwin: shell loaded" 15
    wait_log "qdwin: layer-popup grab handler registered" 5
    sleep 2
    stop_weston
    cp weston.log $out
  '';

  # qdwin's own layer-shell protocol client (qdwin/qdwin/test_zwlr_layer_shell.py,
  # the meson `protocol` suite) against weston-qdistro, in patched-libweston
  # mode: NULL-parent popup accepted, grab-handler registration, popup
  # reposition on a layer-parented popup. The stale-serial grab test SKIPs
  # (headless weston advertises no wl_seat) and a live grab + dismissal
  # needs a real input serial — both VM GUI lane scope.
  qdwin-layer-popup = westonCheck "qdwin-layer-popup" ''
    mkdir -p stage/qdshell stage/qdwin-test
    cp ${../qdwin/qdwin/test_zwlr_layer_shell.py} stage/qdwin-test/test_zwlr_layer_shell.py
    ${pyWayland}/bin/python3 -m pywayland.scanner \
      -i ${wayland-scanner}/share/wayland/wayland.xml \
         ${qdwinXml}/wlr-layer-shell-unstable-v1.xml \
         ${wayland-protocols}/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml \
         ${qdwinXml}/qdwin-shell-v1.xml \
      -o stage/qdshell/protocol
    start_weston wl-proto
    wait_log "qdwin: shell loaded" 15
    rc=0
    ( cd stage/qdwin-test
      QDWIN_USE_VENDORED_LIBWESTON=1 QDWIN_COMPOSITOR_LOG="$OLDPWD/weston.log" \
        QDWIN_WESTON_LOG="$OLDPWD/weston.log" \
        ${pyWayland}/bin/python3 test_zwlr_layer_shell.py wl-proto
    ) > client.log 2>&1 || rc=$?
    [ "$rc" = 0 ] || die "protocol client exit $rc" client.log weston.log
    for t in null_parent_popup layer_popup_grab_handler_registered \
             layer_popup_reposition; do
      grep -q "PASS \[$t\]" client.log || die "$t did not PASS" client.log weston.log
    done
    grep -q "PASS \[null_parent_popup\] vendored" client.log \
      || die "NULL-parent popup took the stock-libweston branch" client.log
    stop_weston client.log
    dump client.log weston.log > $out
  '';

  # weston-qdistro copies nix/weston-delta.nix wholesale onto upstream. Every
  # vendored file that differs from (or is absent in) the upstream tarball
  # must be on that list, and every listed file must still differ — so an
  # edit to an unlisted vendored file can't silently miss the nix compositor.
  # Allowed exceptions are the build/stripping glue and doc files that
  # nix/delta-audit.md excludes on purpose.
  weston-delta-complete =
    let
      listed = builtins.toFile "weston-delta.txt"
        (lib.concatLines (lib.sort lib.lessThan (import ./weston-delta.nix)));
      excluded = builtins.toFile "weston-delta-excluded.txt" (lib.concatLines [
        "README.md"
        "include/libweston/meson.build"
        "meson.build"
        "releasing.md"
      ]);
    in
    runCommand "weston-delta-complete" { } ''
      ln -s ${westonUpstreamSrc} up   # nixpkgs' weston src: unpacked tree
      ( cd ${vendoredSrc} && find . -type f -printf '%P\n' ) | LC_ALL=C sort > vendored.txt
      : > changed.txt
      while IFS= read -r f; do
        if [ ! -e "up/$f" ] || ! cmp -s "${vendoredSrc}/$f" "up/$f"; then
          echo "$f" >> changed.txt
        fi
      done < vendored.txt
      LC_ALL=C sort -o changed.txt changed.txt
      LC_ALL=C comm -23 changed.txt ${excluded} > functional.txt
      LC_ALL=C sort ${listed} > listed.txt
      if ! cmp -s functional.txt listed.txt; then
        echo "nix/weston-delta.nix is out of sync with qdwin/libweston-vendored/src:"
        echo "  vendored files differing from upstream but NOT listed:"
        LC_ALL=C comm -23 functional.txt listed.txt | sed 's/^/    /'
        echo "  listed files identical to upstream (stale entries):"
        LC_ALL=C comm -13 functional.txt listed.txt | sed 's/^/    /'
        exit 1
      fi
      echo "weston delta: $(wc -l < listed.txt) files, in sync"
      cp listed.txt $out
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

  # A real file operation through the installed qfileman: F5 copy via the
  # rsync CommandDialog, driven by nix/smoke/qfileman-copy/sitecustomize.py.
  # PATH holds coreutils only, so rsync must come from the wrapper's own
  # closure (the runtime-tools fix), not the build environment.
  qfileman-copy = runCommand "qfileman-copy" { } ''
    export HOME="$PWD/home" XDG_RUNTIME_DIR="$PWD/xdg"
    mkdir -p "$HOME" "$XDG_RUNTIME_DIR" src dst
    chmod 0700 "$XDG_RUNTIME_DIR"
    head -c 65536 /dev/urandom > src/payload.bin
    rc=0
    env -i HOME="$HOME" XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
      PATH=${coreutils}/bin QT_QPA_PLATFORM=offscreen \
      PYTHONPATH=${./smoke/qfileman-copy} \
      QFM_SMOKE_SRC="$PWD/src/payload.bin" QFM_SMOKE_DEST="$PWD/dst/payload.bin" \
      ${apps.qfileman}/bin/qfileman --no-plugins "$PWD/src" > app.log 2>&1 || rc=$?
    cat app.log
    [ "$rc" = 0 ] || { echo "FAIL: qfileman-copy exit $rc"; exit 1; }
    grep -q "qfm-smoke: OK" app.log
    cmp src/payload.bin dst/payload.bin
    cp app.log $out
  '';

  # End-to-end native stack: qfileman as a real Wayland client of the
  # nix-built weston-qdistro + qdwin. The QFileMan toplevel's own handle
  # must be mapped (normal), with compositor and app alive throughout.
  qfileman-on-qdwin = westonCheck "qfileman-on-qdwin" ''
    start_weston wl-qd
    WAYLAND_DISPLAY=wl-qd QT_QPA_PLATFORM=wayland \
      ${apps.qfileman}/bin/qfileman > app.log 2>&1 &
    apid=$!
    wait_log 'qdwin: toplevel_added handle=[0-9]+ .*title="QFileMan"' 20 app.log
    h=$(grep -oE 'qdwin: toplevel_added handle=[0-9]+ .*title="QFileMan"' weston.log \
          | head -1 | sed -E 's/.*handle=([0-9]+).*/\1/')
    wait_log "qdwin: mapped handle=$h .*\(normal\)" 10 app.log
    sleep 2
    kill -0 "$apid" 2>/dev/null || die "qfileman exited" app.log weston.log
    kill "$apid"; wait "$apid" 2>/dev/null || true
    stop_weston app.log
    dump app.log weston.log > $out
  '';

  # The packaged session shell: bin/qdshell (quickshell + installed QML tree
  # + QML_IMPORT_PATH) under weston-qdistro + qdwin. `qdwin: shell bound`
  # only appears once the QML tree has imported Qdistro.Qdwin from the
  # installed plugin and bound qdwin_shell_v1.
  qdshell-on-qdwin = westonCheck "qdshell-on-qdwin" ''
    start_bus
    start_weston wl-qd
    wait_log "qdwin: shell loaded" 15
    WAYLAND_DISPLAY=wl-qd QT_QPA_PLATFORM=wayland \
      ${apps.qdshell}/bin/qdshell > qs.log 2>&1 &
    spid=$!
    wait_log "qdwin: shell bound" 60 qs.log
    if grep -q 'module "Qdistro.Qdwin" is not installed' qs.log; then
      die "Qdistro.Qdwin did not resolve" qs.log
    fi
    sleep 3
    kill -0 "$spid" 2>/dev/null || die "qdshell exited" qs.log weston.log
    kill "$spid"; wait "$spid" 2>/dev/null || true
    stop_weston qs.log
    kill "$(cat bus.pid)" || true
    dump qs.log weston.log > $out
  '';
}
