"""The QML must ship with the package (todo/iso/14 Phase D).

qdgreeter's UI is QML loaded at runtime from QML_ROOT. When qml/ lived at the
repo top level, `pip install` produced a wheel with no QML at all, and the
greeter died at boot on every pip-installed machine with
"Main.qml: No such file or directory" -- while the VM harness, which copies
the whole source tree to /opt, kept passing. These pin the fix: the QML is
package data, resolvable through importlib.resources, and pyproject declares
every file Main.qml needs.
"""
from __future__ import annotations

import importlib.resources
import re
from pathlib import Path

import qdgreeter.app as app

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_qml_root_is_inside_the_package():
    pkg = Path(app.__file__).resolve().parent
    assert app.QML_ROOT == pkg / "qml"
    assert (app.QML_ROOT / "Main.qml").is_file()
    assert (app.QML_ROOT / "GreetUI.qml").is_file()
    assert (app.QML_ROOT / "shim" / "qmldir").is_file()


def test_qml_is_reachable_as_package_data():
    files = importlib.resources.files("qdgreeter")
    assert (files / "qml" / "Main.qml").is_file()
    assert (files / "qml" / "shim" / "Color.qml").is_file()


def test_pyproject_declares_the_qml_as_package_data():
    text = PYPROJECT.read_text()
    m = re.search(r"\[tool\.setuptools\.package-data\]\s*(?:#[^\n]*\n\s*)*qdgreeter\s*=\s*\[(.*?)\]", text, re.S)
    assert m, "pyproject.toml has no [tool.setuptools.package-data] qdgreeter entry"
    globs = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert {"qml/*.qml", "qml/shim/*.qml", "qml/shim/qmldir"} <= globs
    # nothing may reach outside the package: setuptools drops it silently
    assert not any(g.startswith("..") for g in globs)


def test_every_qml_file_main_imports_is_declared():
    # Main.qml `import shim`; the shim module is its qmldir plus the files it lists
    qmldir = (app.QML_ROOT / "shim" / "qmldir").read_text()
    listed = re.findall(r"(\S+\.qml)\b", qmldir)
    assert listed, qmldir
    for f in listed:
        assert (app.QML_ROOT / "shim" / f).is_file(), f
