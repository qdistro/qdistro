"""Static acceptance checks for the fable-6 Phase 1 SELinux posture."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _rules_only(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_bootstrap_hardened_profiles_require_selinux_enforcing() -> None:
    boot = _read("scripts/install/qdistro-bootstrap.sh")
    assert "QDISTRO_ALLOW_PERMISSIVE=1" in boot
    assert 'target_mode="enforcing"' in boot
    assert "setenforce 1" in boot
    assert "refuses to finish without enforcing" in boot
    assert "requires qdistro SELinux policy before enforcing" in boot
    assert "sed -i 's/^SELINUX=.*/SELINUX=permissive/'" not in boot


def test_tier1_policy_does_not_grant_broad_proc_or_home_file_reads() -> None:
    te = _rules_only(_read("selinux/tier1/qdistro_tier1.te"))
    assert "domain_read_all_domains_state(qdistro_tier1_t)" not in te
    assert "allow qdistro_tier1_t user_home_t:file" not in te
    assert "qdistro_tier1_config_t:file manage_file_perms" in te


def test_session_manager_permissive_tag_is_explicitly_scoped() -> None:
    te = _read("selinux/session_manager/qdistro_session_manager.te")
    assert "permissive qdistro_sessmgr_t;" in _rules_only(te)
    assert "PERMISSIVE rollout" in te
    assert "pin" in te.lower()
