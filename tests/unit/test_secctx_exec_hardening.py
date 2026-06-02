from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SECCTX_C = ROOT / "daemons/secctx-exec/qdistro-secctx-exec.c"


def test_secctx_exec_requires_launcher_or_dev_override():
    src = SECCTX_C.read_text(encoding="utf-8")
    assert "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED" in src
    assert "QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER" in src
    assert "trusted_root_parent" in src
    assert "proc_starttime(pid)" in src
    assert "st_before == st_after" in src
    assert "trusted_root_ancestor" not in src
    assert "trusted_spawn_ancestor" not in src
    assert "proc_cmdline_contains" not in src
    assert "return 13" in src


def test_secctx_exec_validates_identity_shapes_before_wayland_bind():
    src = SECCTX_C.read_text(encoding="utf-8")
    assert "validate_secctx(sandbox_engine, app_id, instance_id)" in src
    assert "qdistro.tier" in src
    assert "validate_token(\"app-id\"" in src


def test_launch_record_publication_is_exclusive_runtime_file():
    src = SECCTX_C.read_text(encoding="utf-8")
    tier3 = (ROOT / "tier3/spawn-tier3.sh").read_text(encoding="utf-8")
    assert "path_under_runtime" in src
    assert "O_EXCL" in src
    assert "O_NOFOLLOW" in src
    assert "QDISTRO_LAUNCH_RECORD_TOKEN" in src
    assert "QDISTRO_LAUNCH_RECORD_TOKEN" in tier3
    assert 'INNER_TOKEN" = "$LAUNCHREC_TOKEN' in tier3
    assert 'LAUNCHREC_PATH="$ADMIN_RUNTIME/qdistro-tier3-launchrec-$LAUNCHREC_TOKEN.pid"' not in tier3
    assert "fopen(lr_path" not in src


def test_spawn_scripts_mark_trusted_secctx_launches():
    scripts = [
        ROOT / "selinux/tier1/spawn-tier1.sh",
        ROOT / "tier2/spawn-tier2.sh",
        ROOT / "tier3/spawn-tier3.sh",
        ROOT / "tier4-vm/spawn-tier4.sh",
        ROOT / "tier5-vm/spawn-tier5.sh",
        ROOT / "tier5b-vm/spawn-tier5b.sh",
    ]
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1" in text, script


def test_trusted_marker_is_scoped_to_secctx_wrapper():
    wrapped_scripts = [
        ROOT / "selinux/tier1/spawn-tier1.sh",
        ROOT / "tier3/spawn-tier3.sh",
        ROOT / "tier4-vm/spawn-tier4.sh",
        ROOT / "tier5-vm/spawn-tier5.sh",
        ROOT / "tier5b-vm/spawn-tier5b.sh",
    ]
    for script in wrapped_scripts:
        text = script.read_text(encoding="utf-8")
        assert "env QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1" in text, script
        assert "exec env QDISTRO_TIER1_TITLE_PREFIX=\"$TITLE_PREFIX\" \\\n    QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1" not in text
        assert "\n    QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \\\n    \"${SECCTX_WRAP[@]}\"" not in text


def test_tier1_tier2_direct_admin_degrades_untagged():
    tier1 = (ROOT / "selinux/tier1/spawn-tier1.sh").read_text(encoding="utf-8")
    tier2 = (ROOT / "tier2/spawn-tier2.sh").read_text(encoding="utf-8")
    assert "running untagged" in tier1
    assert "running un-tagged" in tier2
    assert "secctx stamping now requires" not in tier1
    assert "secctx stamping now requires" not in tier2


def test_vm_secctx_integration_tests_only_forward_dev_override_conditionally():
    tests = [
        ROOT / "tier3/spawn-tier3.sh",
        ROOT / "tests/integration/vm/s44-tier4-secctx-exec.sh",
        ROOT / "tests/integration/vm/s46-tier4-clipboard-gate.sh",
        ROOT / "tests/integration/vm/s110-tier4-waypipe-display.sh",
    ]
    for test in tests:
        text = test.read_text(encoding="utf-8")
        assert "QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1" in text, test
        assert (
            "${QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED:+"
            "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1}"
        ) in text, test
        assert (
            "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED="
            "$QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED"
        ) not in text, test


def test_hardened_broker_profile_fails_closed():
    conf = (ROOT / "deploy/etc/qdistro/broker-hardened.conf").read_text(
        encoding="utf-8"
    )
    assert "secctx_launcher_gated = true" in conf
    assert "lineage_enforce = true" in conf
    assert "identity_strict = true" in conf
    assert "require_silo_active = true" in conf
