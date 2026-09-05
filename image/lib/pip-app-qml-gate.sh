#!/bin/bash
# pip-app-qml-gate.sh — sourced by image/config.sh from the SYNCED tree
# (kiwi imports only scripts into the chroot). Bats-testable on the host.
#
# pip_app_qml_gate <app>... — return 1 (after a FATAL line) unless every
# app's INSTALLED package carries the QML it loads at runtime: qml/Main.qml
# and the `shim` module it imports (qml/shim/qmldir). A wheel that ships the
# Python but not the QML installs fine and dies at first launch: run 28
# booted to a crash-looping greeter that way (todo/iso/14 Phase D), and a
# package-data regression that drops only the shim globs would die with
# "module shim is not installed" the same way (qdlocker's pyproject says so).
#
# python3 -P: never let the CWD (or a script dir) onto sys.path, so a
# source checkout can never stand in for the installed package.
# $QDISTRO_PYTHON overrides the interpreter (tests).
pip_app_qml_gate() {
    local app f found
    for app in "$@"; do
        for f in qml/Main.qml qml/shim/qmldir; do
            found="$("${QDISTRO_PYTHON:-python3}" -P -c "
import importlib.resources as r, functools
p = functools.reduce(lambda a, b: a / b, '$f'.split('/'), r.files('$app'))
print(p if p.is_file() else '')" 2>/dev/null || true)"
            if [ -z "$found" ]; then
                echo "[qdistro-image] FATAL: $app installed without its QML ($f is not inside the installed package);" \
                     "it would crash at first launch. Fix $app's pyproject [tool.setuptools.package-data]. Aborting build." >&2
                return 1
            fi
            echo "[qdistro-image] $app QML present: $found"
        done
    done
}
