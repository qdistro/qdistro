"""Tests for the disposable-class registry + the min_tier enablement gate
(07-disposables-plan P2 — open-in-disposable).

The load-bearing property is the HOSTILE-CLASS gate: pdf / office / archive
must be un-enableable at tier 2 — data-driven (min_tier > MAX_AVAILABLE_TIER)
AND backed by a code-enforced floor so a malformed/edited registry cannot lower
them. These tests pin both, plus the fail-closed parse behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SM_DIR = REPO_ROOT / "session_manager"
sys.path.insert(0, str(SM_DIR))


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


# qdistro_disposable_classes imports qdistro_disposables; load that first so the
# sibling import resolves against the source tree.
_load("qdistro_disposables", SM_DIR / "qdistro_disposables.py")
C = _load("qdistro_disposable_classes", SM_DIR / "qdistro_disposable_classes.py")

SHIPPED_REGISTRY = SM_DIR / "disposable-classes.toml"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "classes.toml"
    p.write_text(body)
    return p


# --- the shipped registry --------------------------------------------------

def test_shipped_registry_loads():
    classes = C.load_classes(SHIPPED_REGISTRY)
    # Low-risk classes are present + enabled at the tier-2 default.
    for n in ("agent-scratch", "text/plain", "url-preview-known-origin"):
        assert n in classes
        assert C.resolve_class(n, classes).name == n
    # Hostile classes are present but DISABLED at tier 2.
    for n in ("pdf", "office", "archive"):
        assert n in classes
        with pytest.raises(C.ClassDisabled):
            C.resolve_class(n, classes)


@pytest.mark.cheat_aware(
    protects="hostile-input classes (pdf/office/archive) are DISABLED at tier "
             "2 — the data-driven min_tier gate keeps them off until VM-tier "
             "disposables exist",
    severity="critical",
    cheats=[
        "lower the shipped registry's min_tier for a hostile class to 2",
        "weaken the assertion to allow the class to resolve at tier 2",
        "raise MAX_AVAILABLE_TIER so the gate no longer fires",
    ],
    consequence="a hostile document parser (pdf/office/archive) runs in a "
                "shared-kernel tier-2 container, selling the Qubes-like "
                "containment promise without the containment",
)
def test_hostile_classes_disabled_at_tier2_enabled_at_tier4():
    """The gate is data-driven AND flips with the tier: hostile classes are
    disabled at the tier-2 default and become enabled only at tier 4 (when
    VM-tier disposables exist)."""
    classes = C.load_classes(SHIPPED_REGISTRY)
    for n in ("pdf", "office", "archive"):
        with pytest.raises(C.ClassDisabled):
            C.resolve_class(n, classes, max_tier=2)
        # At tier 4 (VM-tier) they resolve — proving the gate is the tier, not
        # a hardcoded refusal.
        assert C.resolve_class(n, classes, max_tier=4).name == n


def test_max_available_tier_is_two():
    """P1/P2 ship only tier-2 (podman) disposables; the constant must stay 2
    until the VM-tier path lands (raising it is the deliberate flip)."""
    assert C.MAX_AVAILABLE_TIER == 2


# --- the hostile-class FLOOR (code-enforced, beyond min_tier data) ---------

@pytest.mark.cheat_aware(
    protects="a hostile class can never be configured below its code-enforced "
             "floor — neither a typo nor a malicious local registry edit can "
             "enable pdf/office/archive at tier 2",
    severity="critical",
    cheats=[
        "remove the HOSTILE_CLASS_MIN_TIER floor check",
        "expect load to succeed instead of raising",
    ],
    consequence="an admin or packaging mistake (or attacker with registry "
                "write) lowers a hostile class to tier 2 and it runs in a "
                "shared-kernel container",
)
@pytest.mark.parametrize("hostile", ["pdf", "office", "archive"])
def test_hostile_class_floor_rejects_low_min_tier(tmp_path, hostile):
    reg = _write(tmp_path, f"""
[classes.{hostile}]
workload = "weston-terminal"
tier = 2
min_tier = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="hostile-input class"):
        C.load_classes(reg)


@pytest.mark.parametrize("hostile", ["pdf", "office", "archive"])
def test_hostile_class_floor_rejects_low_tier(tmp_path, hostile):
    # Even with min_tier at the floor, a run tier below the floor is refused.
    reg = _write(tmp_path, f"""
[classes.{hostile}]
workload = "weston-terminal"
tier = 2
min_tier = 4
network = "none"
""")
    with pytest.raises(C.RegistryError, match="hostile-input class"):
        C.load_classes(reg)


def test_hostile_class_floor_allows_vm_tier(tmp_path):
    # The floor permits the legitimate VM-tier configuration.
    reg = _write(tmp_path, """
[classes.pdf]
workload = "pdf-viewer"
tier = 4
min_tier = 4
network = "none"
""")
    classes = C.load_classes(reg)
    assert "pdf" in classes
    with pytest.raises(C.ClassDisabled):
        C.resolve_class("pdf", classes, max_tier=2)
    assert C.resolve_class("pdf", classes, max_tier=4).tier == 4


# --- fail-closed parsing ---------------------------------------------------

def test_unknown_key_rejected(tmp_path):
    # A typo'd key (min_teir) must NOT be silently ignored — it could leave the
    # real min_tier defaulted and enable a class.
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
tier = 2
min_teir = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="unknown key"):
        C.load_classes(reg)


def test_missing_min_tier_rejected(tmp_path):
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
tier = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="min_tier"):
        C.load_classes(reg)


def test_missing_tier_rejected(tmp_path):
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
min_tier = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="tier"):
        C.load_classes(reg)


def test_quoted_min_tier_rejected(tmp_path):
    # A string "2" must not be coerced into an int — coercion could turn a
    # malformed value into a permissive tier.
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
tier = 2
min_tier = "2"
network = "none"
""")
    with pytest.raises(C.RegistryError, match="integer"):
        C.load_classes(reg)


def test_invalid_workload_rejected(tmp_path):
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "Bad Workload"
tier = 2
min_tier = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="workload"):
        C.load_classes(reg)


def test_invalid_network_rejected(tmp_path):
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
tier = 2
min_tier = 2
network = "wide-open"
""")
    with pytest.raises(C.RegistryError, match="network"):
        C.load_classes(reg)


def test_invalid_tier_value_rejected(tmp_path):
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
tier = 3
min_tier = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="tier"):
        C.load_classes(reg)


def test_malformed_toml_raises(tmp_path):
    reg = tmp_path / "broken.toml"
    reg.write_text("this is not [[[ valid toml")
    with pytest.raises(C.RegistryError):
        C.load_classes(reg)


def test_missing_file_raises():
    with pytest.raises(C.RegistryError):
        C.load_classes("/nonexistent/registry.toml")


def test_no_classes_table_raises(tmp_path):
    reg = _write(tmp_path, "# empty\n")
    with pytest.raises(C.RegistryError, match="classes"):
        C.load_classes(reg)


def test_bad_class_name_rejected(tmp_path):
    # A traversal name UNDER [classes] must be rejected (it rides into an
    # action string + could be logged/pathed downstream).
    reg = _write(tmp_path, """
[classes."../evil"]
workload = "weston-terminal"
tier = 2
min_tier = 2
network = "none"
""")
    with pytest.raises(C.RegistryError, match="invalid class name"):
        C.load_classes(reg)


# --- open_action -----------------------------------------------------------

def test_open_action_shape():
    assert C.open_action("agent-scratch") == "qdistro.dispose.open:agent-scratch"
    # A class with a '/' (mime-class) rides into the action verbatim.
    assert C.open_action("text/plain") == "qdistro.dispose.open:text/plain"


def test_open_action_rejects_bad_class():
    with pytest.raises(C.RegistryError):
        C.open_action("BAD CLASS")
    with pytest.raises(C.RegistryError):
        C.open_action("../traversal")


# --- resolve_class errors --------------------------------------------------

def test_resolve_unknown_class():
    classes = C.load_classes(SHIPPED_REGISTRY)
    with pytest.raises(C.UnknownClass):
        C.resolve_class("does-not-exist", classes)


def test_resolve_from_registry_wrapper():
    cls = C.resolve_from_registry("agent-scratch", path=SHIPPED_REGISTRY)
    assert cls.workload == "weston-terminal"
    assert cls.network == "none"


# --- the CLI (the trusted-bash resolver) -----------------------------------

def test_cli_resolve_enabled(tmp_path):
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SM_DIR / "qdistro_disposable_classes.py"),
         "--resolve", "agent-scratch", "--registry", str(SHIPPED_REGISTRY)],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "WORKLOAD=weston-terminal" in out.stdout
    assert "OPEN_ACTION=qdistro.dispose.open:agent-scratch" in out.stdout


def test_cli_resolve_disabled_exit4():
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SM_DIR / "qdistro_disposable_classes.py"),
         "--resolve", "pdf", "--registry", str(SHIPPED_REGISTRY)],
        capture_output=True, text=True)
    assert out.returncode == 4


def test_cli_resolve_unknown_exit3():
    import subprocess
    out = subprocess.run(
        [sys.executable, str(SM_DIR / "qdistro_disposable_classes.py"),
         "--resolve", "nope", "--registry", str(SHIPPED_REGISTRY)],
        capture_output=True, text=True)
    assert out.returncode == 3


def test_cli_resolve_malformed_exit5(tmp_path):
    import subprocess
    bad = tmp_path / "bad.toml"
    bad.write_text("[[[ not toml")
    out = subprocess.run(
        [sys.executable, str(SM_DIR / "qdistro_disposable_classes.py"),
         "--resolve", "pdf", "--registry", str(bad)],
        capture_output=True, text=True)
    assert out.returncode == 5


# --- export-back field (07-disposables-plan P2 / D7 copy-exception) ---------

def test_export_defaults_false(tmp_path):
    reg = _write(tmp_path, """
[classes.noexport]
workload = "weston-terminal"
tier = 2
min_tier = 2
""")
    cls = C.load_classes(reg)["noexport"]
    assert cls.export is False


def test_export_true_parsed(tmp_path):
    reg = _write(tmp_path, """
[classes.withexport]
workload = "weston-terminal"
tier = 2
min_tier = 2
export = true
""")
    assert C.load_classes(reg)["withexport"].export is True


def test_export_non_bool_rejected(tmp_path):
    """A quoted/int export is rejected (fail-closed) — a typo can never silently
    grant an export surface."""
    reg = _write(tmp_path, """
[classes.bad]
workload = "weston-terminal"
tier = 2
min_tier = 2
export = "true"
""")
    with pytest.raises(C.RegistryError):
        C.load_classes(reg)


def test_shipped_agent_scratch_is_export_capable():
    classes = C.load_classes(SHIPPED_REGISTRY)
    assert classes["agent-scratch"].export is True
    # text/plain + url-preview are NOT export-capable in the shipped registry.
    assert classes["text/plain"].export is False
    assert classes["url-preview-known-origin"].export is False


def test_export_action_shape():
    assert C.export_action("agent-scratch") == "qdistro.dispose.export:agent-scratch"
    with pytest.raises(C.RegistryError):
        C.export_action("../evil")


def test_resolver_cli_emits_export_lines(tmp_path):
    import subprocess
    import sys
    reg = _write(tmp_path, """
[classes."agent-scratch"]
workload = "weston-terminal"
tier = 2
min_tier = 2
export = true
""")
    out = subprocess.run(
        [sys.executable, str(C.__file__), "--resolve", "agent-scratch",
         "--registry", str(reg)],
        capture_output=True, text=True, check=True).stdout
    assert "EXPORT=true" in out
    assert "EXPORT_ACTION=qdistro.dispose.export:agent-scratch" in out
