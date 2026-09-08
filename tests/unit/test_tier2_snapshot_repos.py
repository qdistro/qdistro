"""Tier-2 image builds stay on the qdistro Tumbleweed snapshot."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
TIER2 = ROOT / "tier2"
CONTAINERFILES = (
    *sorted(TIER2.glob("Containerfile.*")),
    *sorted((ROOT / "templates" / "recipes").glob("Containerfile.tier2-*")),
)


def _image_snapshot() -> str:
    root = ET.parse(ROOT / "image" / "config.xml").getroot()
    snapshots = []
    for repo in root.findall("repository"):
        url = repo.find("source").get("path", "")
        match = re.search(r"/history/(\d{8})/tumbleweed/repo/", url)
        if match:
            snapshots.append(match.group(1))
    assert snapshots and len(set(snapshots)) == 1
    return snapshots[0]


def test_tier2_snapshot_matches_image_release_pin():
    assert (TIER2 / "SNAPSHOT").read_text().strip() == _image_snapshot()


def test_every_shipped_tier2_recipe_replaces_repos_before_refresh():
    assert len(CONTAINERFILES) == 5
    for path in CONTAINERFILES:
        text = path.read_text()
        configure = text.index("configure-snapshot-repos.sh")
        refresh = text.index("zypper --non-interactive --gpg-auto-import-keys refresh")
        assert "COPY SNAPSHOT configure-snapshot-repos.sh" in text, path
        assert configure < refresh, path
        assert "--no-gpg-checks" not in text, path


def test_url_preview_optional_ca_update_cannot_hide_zypper_failure():
    text = (TIER2 / "Containerfile.url-preview").read_text()
    assert "&& { update-ca-certificates 2>/dev/null || true; }" in text


def test_configurator_rejects_bad_pin_without_replacing_repos(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    inherited = repos / "rolling.repo"
    inherited.write_text("rolling\n")
    bad = tmp_path / "SNAPSHOT"
    bad.write_text("tumbleweed\n")
    proc = subprocess.run(
        ["sh", str(TIER2 / "configure-snapshot-repos.sh"), str(bad)],
        env={**os.environ, "QDISTRO_ZYPP_REPOS_D": str(repos)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert inherited.read_text() == "rolling\n"


def test_configurator_replaces_rolling_repos_with_signed_snapshot(tmp_path):
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "rolling.repo").write_text("rolling\n")
    subprocess.run(
        ["sh", str(TIER2 / "configure-snapshot-repos.sh"), str(TIER2 / "SNAPSHOT")],
        env={**os.environ, "QDISTRO_ZYPP_REPOS_D": str(repos)},
        check=True,
    )
    assert sorted(path.name for path in repos.glob("*.repo")) == [
        "qdistro-snapshot-nonoss.repo",
        "qdistro-snapshot-oss.repo",
    ]
    snapshot = _image_snapshot()
    for path in repos.glob("*.repo"):
        text = path.read_text()
        assert f"https://download.opensuse.org/history/{snapshot}/" in text
        assert "\ngpgcheck=1\n" in text
        assert "tumbleweed:latest" not in text
