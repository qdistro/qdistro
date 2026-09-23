"""Packaging regression tests (finding #21).

A source/wheel install of qfileman must ship its desktop launcher, AppStream
metainfo, and icon so the app integrates with the shell launcher, AppStream
catalog, and icon theme — not just a /usr/bin wrapper.

These tests build a wheel from the repo and assert the data-files land under
the wheel's `*.data/data/share/...` tree (which pip materializes under
<prefix>/share/... on install). They fail before the pyproject
[tool.setuptools.data-files] entries were added and pass after.
"""

import glob
import os
import subprocess
import sys
import zipfile

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_DATA = [
    "share/applications/qfileman.desktop",
    "share/metainfo/qfileman.metainfo.xml",
    "share/icons/hicolor/scalable/apps/qfileman.svg",
]


def _build_wheel(tmp_path):
    out = str(tmp_path / "wheel")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            REPO_ROOT,
            "-w",
            out,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(
            "wheel build unavailable in this environment:\n"
            + proc.stdout
            + proc.stderr
        )
    wheels = glob.glob(os.path.join(out, "qfileman-*.whl"))
    assert wheels, "pip wheel produced no qfileman wheel"
    return wheels[0]


def test_source_assets_exist_in_tree():
    """The asset files referenced by packaging must exist in the repo."""
    for rel in ["qfileman.desktop", "qfileman.metainfo.xml", "icons/qfileman.svg"]:
        assert os.path.isfile(os.path.join(REPO_ROOT, rel)), f"missing in-tree asset: {rel}"


def test_wheel_includes_desktop_metainfo_icon(tmp_path):
    """A built wheel must carry desktop/metainfo/icon under its data tree."""
    whl = _build_wheel(tmp_path)
    names = zipfile.ZipFile(whl).namelist()
    data_entries = {
        n.split(".data/data/", 1)[1] for n in names if ".data/data/" in n
    }
    for expected in EXPECTED_DATA:
        assert expected in data_entries, (
            f"{expected} not packaged in wheel; got data entries: {sorted(data_entries)}"
        )
