"""Tier-2 image builds stay on the qdistro Tumbleweed snapshot."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TIER2 = ROOT / "tier2"
CONTAINERFILES = (
    *sorted(TIER2.glob("Containerfile.*")),
    *sorted((ROOT / "templates" / "recipes").glob("Containerfile.tier2-*")),
)


def _image_snapshot() -> str:
    """The one pin: snapshot= in the repo-root snapshot.conf."""
    values = [
        line.split("=", 1)[1]
        for line in (ROOT / "snapshot.conf").read_text().splitlines()
        if line.startswith("snapshot=")
    ]
    assert len(values) == 1 and re.fullmatch(r"\d{8}", values[0])
    return values[0]


def test_tier2_has_no_pin_of_its_own():
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--error-unmatch", "tier2/SNAPSHOT"],
        capture_output=True,
    )
    assert tracked.returncode != 0


def test_make_script_builds_with_snapshot_conf_pin(tmp_path):
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    seen = tmp_path / "seen"
    podman = fakebin / "podman"
    # the context is the last argument; record the pin staged into it
    podman.write_text(
        f'#!/bin/sh\nfor a; do last="$a"; done\ncat "$last/SNAPSHOT" >> {seen}\n'
    )
    podman.chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["bash", str(TIER2 / "make-tier2-image.sh"), "weston-terminal"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert seen.read_text().strip() == _image_snapshot()
    assert not (TIER2 / "SNAPSHOT").exists()


def test_every_shipped_tier2_recipe_replaces_repos_before_refresh():
    assert len(CONTAINERFILES) == 6
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


def _standalone_tier2(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    standalone = tmp_path / "tier2"
    shutil.copytree(TIER2, standalone)
    # a probe's staged copy carries the pin as SNAPSHOT
    (standalone / "SNAPSHOT").write_text(_image_snapshot() + "\n")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "podman-calls"
    podman = fakebin / "podman"
    podman.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls}\n")
    podman.chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
    return standalone, env, calls


def test_make_script_accepts_standalone_copied_tier2_tree(tmp_path):
    standalone, env, calls = _standalone_tier2(tmp_path)
    proc = subprocess.run(
        ["bash", str(standalone / "make-tier2-image.sh"), "weston-terminal"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Containerfile.weston-terminal" in calls.read_text()


def test_make_script_refuses_staged_pin_that_disagrees_with_snapshot_conf(tmp_path):
    standalone, env, calls = _standalone_tier2(tmp_path)
    shutil.copy2(ROOT / "snapshot.conf", tmp_path / "snapshot.conf")
    (standalone / "SNAPSHOT").write_text("19990101\n")
    proc = subprocess.run(
        ["bash", str(standalone / "make-tier2-image.sh"), "weston-terminal"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "does not match snapshot.conf" in proc.stderr
    assert not calls.exists()


def test_vm_installer_refreshes_policies_coupled_to_build_context():
    installer = ROOT / "scripts" / "install" / "install-templates-for-vm.sh"
    text = installer.read_text()
    assert "if [ ! -f /etc/qdistro/templates/tier2-dev.toml ]" not in text
    assert "if [ ! -f /etc/qdistro/templates/tier2-browser.toml ]" not in text
    assert 'install -m 0644 "$SRC/examples/tier2-dev.toml"' in text
    assert "entrypoint.sh configure-snapshot-repos.sh" in text
    assert '"$UMBRELLA/snapshot.conf"' in text
    assert "> /usr/lib/qdistro/tier2/SNAPSHOT" in text


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
    pin = tmp_path / "SNAPSHOT"
    pin.write_text(_image_snapshot() + "\n")
    subprocess.run(
        ["sh", str(TIER2 / "configure-snapshot-repos.sh"), str(pin)],
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


def _context_listing_podman(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    listing = tmp_path / "context-listing"
    podman = fakebin / "podman"
    podman.write_text(
        "#!/bin/sh\n"
        'for a; do last="$a"; done\n'
        f'{{ echo CONTEXT="$last"; ls -1 "$last"; }} >> {listing}\n'
    )
    podman.chmod(0o755)
    env = {**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"}
    return listing, env, podman


def test_make_script_stages_qfileman_sources_only_for_that_workload(tmp_path):
    listing, env, _ = _context_listing_podman(tmp_path)
    proc = subprocess.run(
        ["bash", str(TIER2 / "make-tier2-image.sh"), "weston-terminal", "qfileman"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    blocks = listing.read_text().split("CONTEXT=")
    # Two CONTEXT blocks: first weston-terminal, second qfileman.
    contexts = [b for b in blocks if b.strip()]
    assert len(contexts) == 2, listing.read_text()
    weston_names = set(contexts[0].split())
    qfileman_names = set(contexts[1].split())
    assert "qdfileman" not in weston_names
    assert "presentation" not in weston_names
    assert "qdfileman" in qfileman_names
    assert "presentation" in qfileman_names
    assert (ROOT / "qdfileman" / "pyproject.toml").exists()
    assert (ROOT / "sdk" / "presentation" / "pyproject.toml").exists()


def test_make_script_qfileman_fails_without_consumer_sources(tmp_path):
    standalone, env, calls = _standalone_tier2(tmp_path)
    proc = subprocess.run(
        ["bash", str(standalone / "make-tier2-image.sh"), "qfileman"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "qfileman sources missing" in proc.stderr
    assert not calls.exists()


def test_make_script_qfileman_accepts_staged_consumer_dir(tmp_path):
    standalone, env, calls = _standalone_tier2(tmp_path)
    consumer = standalone / "consumer"
    consumer.mkdir()
    shutil.copytree(ROOT / "qdfileman", consumer / "qdfileman")
    shutil.copytree(ROOT / "sdk" / "presentation", consumer / "presentation")
    listing = tmp_path / "staged-listing"
    podman = Path(env["PATH"].split(":")[0]) / "podman"
    podman.write_text(
        "#!/bin/sh\n"
        'for a; do last="$a"; done\n'
        f'{{ echo CONTEXT="$last"; ls -1 "$last"; }} >> {listing}\n'
        f'printf \'%s\\n\' "$*" >> {calls}\n'
    )
    proc = subprocess.run(
        ["bash", str(standalone / "make-tier2-image.sh"), "qfileman"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    names = set(listing.read_text().split())
    assert "qdfileman" in names
    assert "presentation" in names


def test_qfileman_containerfile_is_a_presentation_consumer_without_webengine():
    text = (TIER2 / "Containerfile.qfileman").read_text()
    assert "COPY qdfileman /usr/src/qdfileman" in text
    assert "COPY presentation /usr/src/presentation" in text
    assert "python313-PyQt6" in text
    assert "python313-tomli-w" in text
    assert "qt6-wayland" in text
    assert "google-noto-sans-fonts" in text
    assert "pip install --no-deps" in text
    assert "/usr/src/presentation" in text
    assert "/usr/src/qdfileman" in text
    assert 'CMD ["qfileman"]' in text
    assert "import qdistro_presentation, qfileman" in text
    assert "WebEngine" not in text
    assert "qdbrowser" not in text
    assert "qterminator" not in text
    assert "qnotebook" not in text
    assert (TIER2 / "seccomp" / "qfileman.json").is_file()
