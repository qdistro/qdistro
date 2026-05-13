"""qdistro_pwd SELinux policy module sanity tests.

These don't compile or load the module (that requires
selinux-policy-devel + a target SELinux host). They just check
that the on-disk files are present, declare the expected types,
and reference the daemon exec + vault dirs.
"""
from __future__ import annotations

import os
import re
import pytest

POLICY_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "compositor",
                 "spike-6.5", "pwd-policy"))


def _read(name: str) -> str:
    path = os.path.join(POLICY_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_te_declares_required_types():
    te = _read("qdistro_pwd.te")
    assert "policy_module(qdistro_pwd," in te
    for typename in ("qdistro_pwd_t", "qdistro_pwd_exec_t",
                     "qdistro_pwd_var_t", "qdistro_pwd_audit_t"):
        assert re.search(rf"\btype\s+{typename}\s*;", te), \
            f"missing type declaration: {typename}"


def test_te_uses_init_daemon_domain():
    te = _read("qdistro_pwd.te")
    assert "init_daemon_domain(qdistro_pwd_t, qdistro_pwd_exec_t)" in te


def test_te_grants_dbus_acquire_svc_workaround():
    """Tumbleweed's dbus_system_bus_client doesn't include
    acquire_svc; the broker hit this at 0.3.1, the pwd module
    needs the same explicit allow."""
    te = _read("qdistro_pwd.te")
    assert "system_dbusd_t:dbus acquire_svc" in te


def test_te_phase2_is_enforcing():
    """Phase 2 (0.2.0) drops the permissive tag — qdistro_pwd_t is
    fully enforced after the audit2allow harvest. Strip comment lines
    before scanning so historical mentions of the old `permissive`
    declaration in the descriptive header don't false-positive."""
    te = _read("qdistro_pwd.te")
    rules_only = "\n".join(
        ln for ln in te.splitlines() if not ln.lstrip().startswith("#"))
    assert "permissive qdistro_pwd_t;" not in rules_only, \
        "expected the `permissive qdistro_pwd_t;` rule to be removed in Phase 2"
    # Module version bumped to 0.2.0.
    assert "policy_module(qdistro_pwd, 0.2.0)" in te


def test_te_phase2_grants_sys_ptrace():
    """Phase 2: cross-uid /proc/<other-uid>/exe readlink needs
    self:capability sys_ptrace even with the unit's
    AmbientCapabilities=CAP_SYS_PTRACE — the SELinux check
    happens before the kernel cap check."""
    te = _read("qdistro_pwd.te")
    assert "sys_ptrace" in te


def test_te_phase2_grants_cgroup_and_certs():
    """Phase 2 audit2allow harvest: /proc/<pid>/cgroup readers
    need cgroup_t access; Python ssl module init pulls cert_t."""
    te = _read("qdistro_pwd.te")
    assert "cgroup_t" in te
    # cert_t access via the refpolicy interface (preferred over raw allow).
    assert "miscfiles_read_generic_certs(qdistro_pwd_t)" in te


def test_te_grants_var_lib_access():
    te = _read("qdistro_pwd.te")
    # Vault + audit dirs need manage_*_perms.
    assert "qdistro_pwd_var_t:dir   manage_dir_perms" in te
    assert "qdistro_pwd_audit_t:dir   manage_dir_perms" in te


def test_te_optional_tpm_and_polkit():
    """tpm + polkit chats live under optional_policy so the module
    loads cleanly on hosts without those refpolicy modules."""
    te = _read("qdistro_pwd.te")
    assert "dev_rw_tpm(qdistro_pwd_t)" in te
    assert "policykit_dbus_chat(qdistro_pwd_t)" in te
    # Both must be wrapped in optional_policy.
    assert "optional_policy(`" in te


def test_fc_labels_exec_and_dirs():
    fc = _read("qdistro_pwd.fc")
    assert ("/usr/libexec/qdistro/qdistro_pwd_daemon" in fc and
            "qdistro_pwd_exec_t" in fc)
    assert ("/var/lib/qdistro/vaults" in fc and
            "qdistro_pwd_var_t" in fc)
    assert ("/var/lib/qdistro/audit" in fc and
            "qdistro_pwd_audit_t" in fc)


def test_if_exposes_dbus_chat_interface():
    iff = _read("qdistro_pwd.if")
    assert "interface(`qdistro_pwd_dbus_chat'" in iff
    assert "interface(`qdistro_pwd_read_audit'" in iff
    # Mirrors broker pattern: caller<->daemon + dbus daemon proxy edges.
    assert "allow $1 qdistro_pwd_t:dbus send_msg;" in iff
    assert "allow qdistro_pwd_t $1:dbus send_msg;" in iff


def test_makefile_targets_present():
    mk = _read("Makefile")
    for target in ("all", "install", "remove", "clean"):
        assert re.search(rf"^{target}:", mk, re.MULTILINE), \
            f"Makefile missing target: {target}"


def test_install_script_executable():
    path = os.path.join(POLICY_DIR, "install-policy.sh")
    assert os.access(path, os.X_OK), \
        f"install-policy.sh not executable: {path}"


def test_install_script_handles_missing_devel():
    sh = _read("install-policy.sh")
    assert "/usr/share/selinux/devel" in sh
    # Must SKIP cleanly when devel is missing (host without
    # selinux-policy-devel).
    assert 'SKIP' in sh and 'exit 0' in sh
