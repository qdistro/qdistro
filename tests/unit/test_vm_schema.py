"""Unit tests for qdistro_vm_schema — validation of VM-backed silo guest
definitions, qdistro image manifests, and the fail-closed activation guard.

Grounded in doc/vm-definitions.md, doc/isolation-tiers.md (tiers 4-5),
doc/resources.md, and doc/workflows.md (§VM Build Workflow).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_vm_schema import (  # noqa: E402
    MetadataSchemaError,
    RESERVED_GUEST_LANGUAGES,
    RESERVED_PUBLISHER_MODES,
    VM_TIERS,
    check_guest_services_match_manifest,
    check_guest_services_match_manifest_or_raise,
    tumbleweed_image_manifest,
    validate_exposed_services,
    validate_guest,
    validate_image_manifest,
    validate_image_manifest_or_raise,
    validate_isolation,
    validate_publisher,
    validate_vm_silo_spec,
    validate_vm_silo_spec_or_raise,
)


def errs(res):
    return res.errors


# --------------------------------------------------------------------------
# canonical manifest (doc/vm-definitions.md §Resource Reference Shape)
# --------------------------------------------------------------------------

def _good_spec():
    return {
        "spec": {
            "isolation": {
                "tier": 5,
                "backend": "libvirt-qemu",
                "display": "waypipe-vsock",
            },
            "guest": {
                "language": "nixos-module",
                "system": "x86_64-linux",
                "flakeRef": "git+file:///srv/qdistro-vms#firefox-work-vm",
                "lockRef": "git+file:///srv/qdistro-vms?rev=abc123",
                "module": "./vms/firefox-work-vm.nix",
                "output": "nixosConfigurations.firefox-work-vm",
                "exposedServices": [
                    {"name": "waypipe", "vsockPort": 7879},
                ],
            },
            "publisher": {
                "mode": "per-app",
                "command": "/run/current-system/sw/bin/firefox",
                "arbitraryCommand": False,
            },
        },
    }


def _good_image_manifest():
    return {
        "apiVersion": "resources.qdistro.io/v1alpha1",
        "kind": "Image",
        "metadata": {"name": "firefox-work-vm"},
        "definition": {
            "language": "nixos-module",
            "flakeRef": "git+file:///srv/qdistro-vms#firefox-work-vm",
            "lockRef": "git+file:///srv/qdistro-vms?rev=abc123",
            "module": "./vms/firefox-work-vm.nix",
            "output": "nixosConfigurations.firefox-work-vm",
        },
        "build": {
            "builder": "qdistro-image-builder@host",
            "command": ["nix", "build", ".#firefox-work-vm"],
            "sandboxed": True,
            "network": False,
        },
        "output": {
            "path": "/srv/qdistro-images/firefox-work-vm.qcow2",
            "digest": "sha256:" + "0123456789abcdef" * 4,
        },
        "healthChecks": [
            {"name": "publisher-up"},
            {"name": "firefox-launches"},
        ],
    }


# --------------------------------------------------------------------------
# spec.isolation
# --------------------------------------------------------------------------

def test_isolation_ok():
    assert validate_isolation(_good_spec()["spec"]["isolation"]).ok


def test_isolation_must_be_mapping():
    assert not validate_isolation(["tier", 5]).ok


@pytest.mark.parametrize("tier", sorted(VM_TIERS))
def test_isolation_vm_tiers_accepted(tier):
    assert validate_isolation({"tier": tier}).ok


@pytest.mark.parametrize("tier", [0, 1, 2, 3, 6, 7])
def test_isolation_non_vm_tier_rejected(tier):
    res = validate_isolation({"tier": tier})
    assert not res.ok
    assert any("not a VM-backed tier" in e for e in errs(res))


def test_isolation_tier_bool_rejected():
    res = validate_isolation({"tier": True})
    assert not res.ok
    assert any("must be an integer" in e for e in errs(res))


def test_isolation_tier_missing_rejected():
    assert not validate_isolation({}).ok


def test_isolation_unknown_backend_rejected():
    res = validate_isolation({"tier": 5, "backend": "xen"})
    assert not res.ok
    assert any("backend" in e for e in errs(res))


def test_isolation_unknown_display_rejected():
    res = validate_isolation({"tier": 5, "display": "rdp"})
    assert not res.ok
    assert any("display" in e for e in errs(res))


def test_isolation_backend_display_optional():
    assert validate_isolation({"tier": 4}).ok


# --------------------------------------------------------------------------
# spec.guest
# --------------------------------------------------------------------------

def test_guest_ok():
    assert validate_guest(_good_spec()["spec"]["guest"]).ok


def test_guest_must_be_mapping():
    assert not validate_guest("nixos").ok


def test_guest_language_required():
    res = validate_guest({"flakeRef": "x", "lockRef": "y", "output": "z"})
    assert not res.ok
    assert any("language is required" in e for e in errs(res))


def test_guest_language_unknown_rejected():
    res = validate_guest({"language": "docker"})
    assert not res.ok
    assert any("not a reserved guest" in e for e in errs(res))


@pytest.mark.parametrize("lang", sorted(RESERVED_GUEST_LANGUAGES))
def test_guest_languages_accepted_shape(lang):
    guest = {"language": lang}
    if lang in {"nixos-module", "nixos-flake"}:
        guest.update(flakeRef="r", lockRef="l", output="o")
    assert validate_guest(guest).ok


def test_guest_nix_requires_flake_lock_output():
    res = validate_guest({"language": "nixos-module"})
    assert not res.ok
    for fld in ("flakeRef", "lockRef", "output"):
        assert any(f"spec.guest.{fld} is required" in e for e in errs(res))


def test_guest_tumbleweed_does_not_require_nix_refs():
    assert validate_guest({"language": "tumbleweed-script"}).ok


def test_guest_ref_field_wrong_type():
    res = validate_guest({"language": "tumbleweed-script", "module": 123})
    assert not res.ok
    assert any("module must be a string" in e for e in errs(res))


# --------------------------------------------------------------------------
# spec.guest.exposedServices
# --------------------------------------------------------------------------

def test_exposed_services_none_ok():
    assert validate_exposed_services(None).ok


def test_exposed_services_must_be_list():
    assert not validate_exposed_services({"name": "x"}).ok


def test_exposed_service_ok():
    assert validate_exposed_services([{"name": "waypipe", "vsockPort": 7879}]).ok


def test_exposed_service_bad_name():
    res = validate_exposed_services([{"name": "Bad_Name", "vsockPort": 1}])
    assert not res.ok
    assert any("DNS-1123" in e for e in errs(res))


def test_exposed_service_port_not_int():
    res = validate_exposed_services([{"name": "waypipe", "vsockPort": "7879"}])
    assert not res.ok
    assert any("vsockPort must be an integer" in e for e in errs(res))


def test_exposed_service_port_bool_rejected():
    res = validate_exposed_services([{"name": "waypipe", "vsockPort": True}])
    assert not res.ok


def test_exposed_service_duplicate_name():
    res = validate_exposed_services([
        {"name": "waypipe", "vsockPort": 1},
        {"name": "waypipe", "vsockPort": 2},
    ])
    assert not res.ok
    assert any("duplicate service name" in e for e in errs(res))


def test_exposed_service_duplicate_port():
    res = validate_exposed_services([
        {"name": "a", "vsockPort": 1},
        {"name": "b", "vsockPort": 1},
    ])
    assert not res.ok
    assert any("duplicate vsockPort" in e for e in errs(res))


# --------------------------------------------------------------------------
# spec.publisher
# --------------------------------------------------------------------------

def test_publisher_none_ok():
    assert validate_publisher(None).ok


def test_publisher_ok():
    assert validate_publisher(_good_spec()["spec"]["publisher"]).ok


def test_publisher_mode_required():
    res = validate_publisher({"command": "/bin/x"})
    assert not res.ok
    assert any("mode is required" in e for e in errs(res))


def test_publisher_mode_unknown():
    res = validate_publisher({"mode": "weird", "command": "/bin/x"})
    assert not res.ok
    assert any("not a reserved publisher mode" in e for e in errs(res))


@pytest.mark.parametrize("mode", sorted(RESERVED_PUBLISHER_MODES))
def test_publisher_modes_accepted(mode):
    assert validate_publisher({"mode": mode, "command": "/bin/x"}).ok


def test_publisher_command_required():
    res = validate_publisher({"mode": "per-app"})
    assert not res.ok
    assert any("command is required" in e for e in errs(res))


def test_publisher_arbitrary_must_be_bool():
    res = validate_publisher(
        {"mode": "per-app", "command": "/bin/x", "arbitraryCommand": "no"}
    )
    assert not res.ok
    assert any("arbitraryCommand must be a boolean" in e for e in errs(res))


# --------------------------------------------------------------------------
# validate_vm_silo_spec (aggregate)
# --------------------------------------------------------------------------

def test_vm_silo_spec_ok():
    res = validate_vm_silo_spec(_good_spec())
    assert res.ok, res.errors


def test_vm_silo_spec_requires_spec():
    res = validate_vm_silo_spec({})
    assert not res.ok
    assert any("spec is required" in e for e in errs(res))


def test_vm_silo_spec_requires_isolation():
    res = validate_vm_silo_spec({"spec": {"guest": {"language": "tumbleweed-script"}}})
    assert not res.ok
    assert any("spec.isolation is required" in e for e in errs(res))


def test_vm_silo_spec_vm_tier_requires_guest():
    m = _good_spec()
    del m["spec"]["guest"]
    res = validate_vm_silo_spec(m)
    assert not res.ok
    assert any("spec.guest is required" in e for e in errs(res))


def test_vm_silo_spec_or_raise():
    validate_vm_silo_spec_or_raise(_good_spec())
    with pytest.raises(MetadataSchemaError):
        validate_vm_silo_spec_or_raise({"spec": {}})


# --------------------------------------------------------------------------
# image manifest
# --------------------------------------------------------------------------

def test_image_manifest_ok():
    res = validate_image_manifest(_good_image_manifest())
    assert res.ok, res.errors


def test_image_manifest_kind_must_be_image():
    m = _good_image_manifest()
    m["kind"] = "Silo"
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("kind must be" in e for e in errs(res))


def test_image_manifest_requires_builder():
    m = _good_image_manifest()
    del m["build"]["builder"]
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("builder" in e for e in errs(res))


def test_image_manifest_command_string_ok():
    m = _good_image_manifest()
    m["build"]["command"] = "nix build .#firefox-work-vm"
    assert validate_image_manifest(m).ok


def test_image_manifest_command_empty_list_rejected():
    m = _good_image_manifest()
    m["build"]["command"] = []
    res = validate_image_manifest(m)
    assert not res.ok


def test_image_manifest_bad_digest():
    m = _good_image_manifest()
    m["output"]["digest"] = "not-a-digest"
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("content digest" in e for e in errs(res))


def test_image_manifest_truncated_sha256_rejected():
    m = _good_image_manifest()
    m["output"]["digest"] = "sha256:deadbeef"  # 8 hex, not 64
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("content digest" in e for e in errs(res))


def test_image_manifest_full_sha512_ok():
    m = _good_image_manifest()
    m["output"]["digest"] = "sha512:" + "ab" * 64  # 128 hex
    assert validate_image_manifest(m).ok


def test_image_manifest_requires_output_path():
    m = _good_image_manifest()
    del m["output"]["path"]
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("output.path" in e for e in errs(res))


def test_image_manifest_health_checks_required():
    m = _good_image_manifest()
    m["healthChecks"] = []
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("healthChecks" in e for e in errs(res))


def test_image_manifest_health_check_duplicate():
    m = _good_image_manifest()
    m["healthChecks"] = [{"name": "x"}, {"name": "x"}]
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("duplicate name" in e for e in errs(res))


def test_image_manifest_nix_requires_locked_inputs():
    m = _good_image_manifest()
    del m["definition"]["lockRef"]
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("lockRef is required" in e for e in errs(res))


def test_image_manifest_tumbleweed_requires_packages_sourcerefs():
    m = _good_image_manifest()
    m["definition"] = {"language": "tumbleweed-script"}
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("definition.packages is required" in e for e in errs(res))
    assert any("definition.sourceRefs is required" in e for e in errs(res))


def test_image_manifest_packages_must_be_string_list():
    m = _good_image_manifest()
    m["definition"] = {
        "language": "tumbleweed-script",
        "packages": "cups",  # a bare string, not a list
        "sourceRefs": [123],  # non-string entry
    }
    res = validate_image_manifest(m)
    assert not res.ok
    assert any("definition.packages must be a list" in e for e in errs(res))
    assert any("definition.sourceRefs must be a list" in e for e in errs(res))


def test_image_manifest_nix_packages_optional_but_typed():
    # A nix definition may carry packages too; if present it must be well-formed.
    m = _good_image_manifest()
    m["definition"]["packages"] = "firefox"
    res = validate_image_manifest(m)
    assert not res.ok


def test_image_manifest_or_raise():
    validate_image_manifest_or_raise(_good_image_manifest())
    with pytest.raises(MetadataSchemaError):
        validate_image_manifest_or_raise({"kind": "Image"})


# --------------------------------------------------------------------------
# tumbleweed_image_manifest emitter
# --------------------------------------------------------------------------

def test_tumbleweed_image_manifest_emits_valid():
    m = tumbleweed_image_manifest(
        name="print-vm",
        packages=["cups", "waypipe"],
        source_refs=["obs://qdistro/print-vm"],
        build_command=["build-print-image.sh"],
        builder="root@build-host",
        output_path="/srv/qdistro-images/print-vm.qcow2",
        output_digest="sha256:" + "deadbeef" * 8,
        health_checks=[{"name": "cups-up"}],
    )
    assert validate_image_manifest(m).ok
    assert m["kind"] == "Image"
    assert m["definition"]["language"] == "tumbleweed-script"
    assert m["definition"]["packages"] == ["cups", "waypipe"]
    assert m["definition"]["sourceRefs"] == ["obs://qdistro/print-vm"]


def test_tumbleweed_image_manifest_rejects_string_packages():
    # A bare string must raise, not be char-split into a package list.
    with pytest.raises(MetadataSchemaError):
        tumbleweed_image_manifest(
            name="print-vm",
            packages="cups",  # type: ignore[arg-type]
            source_refs=["obs://x"],
            build_command="build.sh",
            builder="root@host",
            output_path="/srv/x.qcow2",
            output_digest="sha256:" + "ab" * 32,
            health_checks=[{"name": "x"}],
        )


def test_tumbleweed_image_manifest_rejects_bad_digest():
    with pytest.raises(MetadataSchemaError):
        tumbleweed_image_manifest(
            name="print-vm",
            packages=["cups"],
            source_refs=["obs://x"],
            build_command="build.sh",
            builder="root@host",
            output_path="/srv/x.qcow2",
            output_digest="bogus",
            health_checks=[{"name": "x"}],
        )


# --------------------------------------------------------------------------
# activation guard — fail closed
# --------------------------------------------------------------------------

def test_services_match_ok():
    spec_guest = _good_spec()["spec"]["guest"]
    guest_exposed = [{"name": "waypipe", "vsockPort": 7879}]
    assert check_guest_services_match_manifest(spec_guest, guest_exposed).ok


def test_services_bare_name_blocks_when_manifest_pins_port():
    # The manifest pins 7879; a port-less guest report cannot confirm it.
    spec_guest = _good_spec()["spec"]["guest"]  # waypipe @ 7879
    res = check_guest_services_match_manifest(spec_guest, ["waypipe"])
    assert not res.ok
    assert any("omits a valid port" in e for e in errs(res))
    res2 = check_guest_services_match_manifest(spec_guest, [{"name": "waypipe"}])
    assert not res2.ok


def test_services_extra_service_blocks():
    spec_guest = _good_spec()["spec"]["guest"]
    guest_exposed = [
        {"name": "waypipe", "vsockPort": 7879},
        {"name": "sshd", "vsockPort": 22},
    ]
    res = check_guest_services_match_manifest(spec_guest, guest_exposed)
    assert not res.ok
    assert any("sshd" in e and "not declared" in e for e in errs(res))


def test_services_port_mismatch_blocks():
    spec_guest = _good_spec()["spec"]["guest"]
    guest_exposed = [{"name": "waypipe", "vsockPort": 9999}]
    res = check_guest_services_match_manifest(spec_guest, guest_exposed)
    assert not res.ok
    assert any("port" in e for e in errs(res))


def test_services_empty_allowset_blocks_any_exposed():
    # A valid guest that declares no exposedServices: any exposed service blocks.
    spec_guest = {"language": "tumbleweed-script"}  # no exposedServices
    res = check_guest_services_match_manifest(spec_guest, [{"name": "waypipe"}])
    assert not res.ok
    assert any("not declared" in e for e in errs(res))


def test_services_empty_allowset_empty_guest_ok():
    spec_guest = {"language": "tumbleweed-script"}
    assert check_guest_services_match_manifest(spec_guest, []).ok


def test_services_fail_closed_on_invalid_guest_block():
    # A guest block that is itself structurally invalid (nixos-module without
    # the required flake refs) blocks activation, even with empty discovery.
    spec_guest = {"language": "nixos-module"}
    res = check_guest_services_match_manifest(spec_guest, [])
    assert not res.ok
    assert any("structurally invalid" in e for e in errs(res))


def test_services_fail_closed_on_none_discovery():
    # Missing discovery data must block, not be read as "zero services".
    spec_guest = {"language": "tumbleweed-script"}
    res = check_guest_services_match_manifest(spec_guest, None)
    assert not res.ok
    assert any("unknown" in e for e in errs(res))


def test_services_fail_closed_on_bad_spec_guest():
    res = check_guest_services_match_manifest(None, [{"name": "waypipe"}])
    assert not res.ok
    assert any("activation blocked" in e for e in errs(res))


def test_services_fail_closed_on_malformed_allowset():
    # A structurally invalid allow-set must block rather than silently widen.
    spec_guest = {"exposedServices": [{"name": "waypipe", "vsockPort": True}]}
    res = check_guest_services_match_manifest(spec_guest, [{"name": "waypipe"}])
    assert not res.ok
    assert any("structurally invalid" in e for e in errs(res))


def test_services_fail_closed_on_duplicate_allowset_name():
    spec_guest = {"exposedServices": [
        {"name": "waypipe", "vsockPort": 1},
        {"name": "waypipe", "vsockPort": 2},
    ]}
    res = check_guest_services_match_manifest(spec_guest, [{"name": "waypipe"}])
    assert not res.ok


def test_services_fail_closed_on_malformed_guest_port():
    spec_guest = _good_spec()["spec"]["guest"]
    res = check_guest_services_match_manifest(
        spec_guest, [{"name": "waypipe", "vsockPort": True}]
    )
    assert not res.ok
    assert any("malformed" in e for e in errs(res))


def test_services_fail_closed_on_string_guest_port():
    spec_guest = _good_spec()["spec"]["guest"]
    res = check_guest_services_match_manifest(
        spec_guest, [{"name": "waypipe", "vsockPort": "7879"}]
    )
    assert not res.ok


def test_services_fail_closed_on_bad_guest_exposed_type():
    spec_guest = _good_spec()["spec"]["guest"]
    res = check_guest_services_match_manifest(spec_guest, "waypipe")
    assert not res.ok


def test_services_fail_closed_on_bad_exposed_entry():
    spec_guest = _good_spec()["spec"]["guest"]
    res = check_guest_services_match_manifest(spec_guest, [42])
    assert not res.ok


def test_services_match_or_raise():
    spec_guest = _good_spec()["spec"]["guest"]
    check_guest_services_match_manifest_or_raise(
        spec_guest, [{"name": "waypipe", "vsockPort": 7879}]
    )
    with pytest.raises(MetadataSchemaError):
        check_guest_services_match_manifest_or_raise(
            spec_guest, [{"name": "sshd", "vsockPort": 22}]
        )
