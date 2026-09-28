"""The image skill reaches silo homes without a checkout or admin role."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from qdistro_silo_skill import install_skill


ROOT = Path(__file__).resolve().parents[2]


def test_installs_per_uid_and_is_idempotent(tmp_path, monkeypatch):
    uid = os.getuid()
    if uid <= 1000:
        # An admin-uid runner (the VM's admin is 1000) must still exercise the
        # install path: files it creates are reported as owned by a silo uid.
        runner, uid = tmp_path.stat().st_uid, 1001
        real_stat = Path.stat

        def as_silo_uid(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if st.st_uid != runner:
                return st
            fields = list(st)
            fields[stat.ST_UID] = uid
            return os.stat_result(fields)

        monkeypatch.setattr(Path, "stat", as_silo_uid)
    home = tmp_path / "silo"
    home.mkdir(mode=0o700)
    source = ROOT / "agents/skills/silo/SKILL.md"
    install_skill(home, uid, source)
    install_skill(home, uid, source)
    target = home / ".agents/skills/qdistro-silo/SKILL.md"
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_uid == uid
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert not target.is_symlink()
    assert "admin approval skill" not in target.read_text().lower()


def test_rejects_admin_and_silo_redirect(tmp_path, monkeypatch):
    home = tmp_path / "silo"
    home.mkdir()
    source = ROOT / "agents/skills/silo/SKILL.md"
    with pytest.raises(ValueError, match="non-admin"):
        install_skill(home, 1000, source)

    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".agents").symlink_to(outside, target_is_directory=True)
    # The test runner may itself be admin uid 1000. Model a valid silo-owned
    # home so the symlink check is exercised on every host.
    real_stat = Path.stat

    def silo_home_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == home:
            return SimpleNamespace(st_mode=result.st_mode, st_uid=1001)
        return result

    monkeypatch.setattr(Path, "stat", silo_home_stat)
    with pytest.raises(ValueError, match="symlink"):
        install_skill(home, 1001, source)
    assert list(outside.iterdir()) == []


def test_image_installers_use_packaged_skill():
    installer = (ROOT / "scripts/install/install-session-manager.sh").read_text()
    manager = (ROOT / "session_manager/qdistro_session_manager.py").read_text()
    tier3 = (ROOT / "scripts/install/install-tier3-for-vm.sh").read_text()
    assert '"$SRC/qdistro_silo_skill.py"' in installer
    assert '/usr/share/qdistro/agents/skills/silo' in installer
    assert '/usr/libexec/qdistro/qdistro_silo_skill.py' in manager
    assert '/usr/libexec/qdistro/qdistro_silo_skill.py' in tier3
    assert '/root/qdistro-src' not in (ROOT / "session_manager/qdistro_silo_skill.py").read_text()
