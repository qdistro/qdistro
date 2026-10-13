# Shared checkPhase for the PyQt components — the nix-sandbox equivalent of
# qci's host container (ci/containers/enter-host.sh):
#   - offscreen Qt, throwaway HOME
#   - a private dbus-daemon from the store's session.conf (dbus-run-session
#     needs /etc/dbus-1), aliased as the system bus like enter-host.sh;
#     pid tracked and killed after the run
#   - short, per-build --basetemp ($QD_PYTEST_TMP): tmp_path under an
#     unsandboxed build dir (/nix/var/nix/builds/<hash>/) blows the 108-byte
#     AF_UNIX limit for socket tests, and a fixed /tmp path collides between
#     concurrent builds when the sandbox is off
#   - faulthandler_timeout + exit_on_timeout: a wedged test (e.g. a modal
#     QMessageBox nobody can click offscreen) dumps its stack into the build
#     log and FAILS the build instead of hanging it forever
#   - QML import path for components whose tests load QML
# $QD_PYTEST_EXTRA (set in a package's preCheck, word-split) adds runtime
# arguments — e.g. a --deselect that only applies when the build environment
# lacks something, so the test still runs where the environment has it.
# perFile = glob: one pytest process per matching file (qci's mode for
# qdbrowser, bounding QtWebEngine residue); fails if the glob is empty.
{ dbus, qt6 }:
{
  args ? "",
  perFile ? null,
}:
let
  pytest = "python -m pytest --basetemp=\"$QD_PYTEST_TMP/bt\" -o faulthandler_timeout=120 -o faulthandler_exit_on_timeout=true $QD_PYTEST_EXTRA";
in
''
  runHook preCheck
  QD_PYTEST_TMP=$(mktemp -d "$TMPDIR/pt.XXXXXX")
  if [ "''${#QD_PYTEST_TMP}" -gt 40 ]; then
    QD_PYTEST_TMP=$(mktemp -d /tmp/qdpt.XXXXXX)
  fi
  export QD_PYTEST_TMP
  QD_PYTEST_EXTRA=''${QD_PYTEST_EXTRA:-}
  export QT_QPA_PLATFORM=offscreen
  export HOME="$TMPDIR/home"
  mkdir -p "$HOME"
  export QML_IMPORT_PATH="${qt6.qtdeclarative}/${qt6.qtbase.qtQmlPrefix}''${QML_IMPORT_PATH:+:$QML_IMPORT_PATH}"
  dbus-daemon --fork --print-address=3 --print-pid=4 \
    --config-file=${dbus}/share/dbus-1/session.conf 3>"$TMPDIR/bus.addr" 4>"$TMPDIR/bus.pid"
  export DBUS_SESSION_BUS_ADDRESS="$(cat "$TMPDIR/bus.addr")"
  export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  rc=0
''
+ (
  if perFile == null then
    ''
      ${pytest} ${args} || rc=$?
    ''
  else
    ''
      n=0
      for t in ${perFile}; do
        [ -e "$t" ] || continue
        n=$((n + 1))
        echo "=== $t"
        ${pytest} ${args} "$t" || rc=1
      done
      [ "$n" -gt 0 ] || { echo "no test files matched ${perFile}" >&2; rc=1; }
    ''
)
+ ''
  kill "$(cat "$TMPDIR/bus.pid")" || true
  # Kept on failure for --keep-failed inspection.
  [ "$rc" -ne 0 ] || rm -rf "$QD_PYTEST_TMP"
  [ "$rc" -eq 0 ]
  runHook postCheck
''
