"""Hostile/invalid-input table for qdistro_vm_schema.

Schema validation runs on UNTRUSTED silo manifest content arriving via the
broker or from CI tooling. The validators must be fail-closed: every hostile
input below must either produce errors (res.ok == False) or parse correctly
for the truly-valid cases — they must never crash, and invalid inputs must
never produce a silent allow (res.ok == True).

cheat_aware markers are on fail-closed cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_vm_schema import (  # noqa: E402
    MetadataSchemaError,
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


def errs(res) -> list[str]:
    return res.errors


def ok(res) -> bool:
    return res.ok


# ---------------------------------------------------------------------------
# helpers — canonical good inputs
# ---------------------------------------------------------------------------

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
                "flakeRef": "git+file:///srv/qdistro-vms#work-vm",
                "lockRef": "git+file:///srv/qdistro-vms?rev=abc123",
                "module": "./vms/work-vm.nix",
                "output": "nixosConfigurations.work-vm",
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


def _good_image():
    return {
        "apiVersion": "resources.qdistro.io/v1alpha1",
        "kind": "Image",
        "metadata": {"name": "test-vm"},
        "definition": {
            "language": "nixos-module",
            "flakeRef": "git+file:///srv/qdistro-vms#test-vm",
            "lockRef": "git+file:///srv/qdistro-vms?rev=abc123",
            "output": "nixosConfigurations.test-vm",
        },
        "build": {
            "builder": "qdistro-builder@host",
            "command": ["nix", "build", ".#test-vm"],
            "sandboxed": True,
            "network": False,
        },
        "output": {
            "path": "/srv/images/test-vm.qcow2",
            "digest": "sha256:" + "0123456789abcdef" * 4,
        },
        "healthChecks": [{"name": "publisher-up"}],
    }


# ---------------------------------------------------------------------------
# SECTION 1: validate_isolation — hostile inputs
# ---------------------------------------------------------------------------

class TestIsolationHostile:

    @pytest.mark.cheat_aware(
        protects="isolation validator rejects every non-VM tier as invalid",
        severity="high",
        cheats=["only check tier in VM_TIERS, not the type"],
        consequence="non-VM silo gets a VM-tier isolation block, skipping VM-specific checks",
    )
    @pytest.mark.parametrize("bad_tier", [
        None, "", "4", "5", True, False, 4.0, 5.0, -1, 0, 3, 6, 100,
        [], {}, object(),
    ])
    def test_invalid_tier_rejected(self, bad_tier):
        """Every non-VM-tier value for tier must be rejected."""
        res = validate_isolation({"tier": bad_tier})
        # True/False are bool subclasses of int — must still be rejected
        assert not ok(res), f"tier={bad_tier!r} should be rejected"

    def test_isolation_not_a_mapping_rejected(self):
        for bad in [None, [], "tier=5", 42, True]:
            assert not ok(validate_isolation(bad)), f"{bad!r} should be rejected"

    @pytest.mark.parametrize("backend", [
        "xen", "vmware", "virtualbox", "kvm", "docker", "", None,
        "libvirt-qemu " + "x" * 1000,  # whitespace suffix
        True, 42,
    ])
    def test_invalid_backend_rejected(self, backend):
        """Unrecognised/typed backend values must be rejected."""
        if backend is None:
            # None means absent — that's ok (backend is optional)
            assert ok(validate_isolation({"tier": 5}))
        else:
            res = validate_isolation({"tier": 5, "backend": backend})
            assert not ok(res), f"backend={backend!r} should be rejected"

    @pytest.mark.parametrize("display", [
        "rdp", "vnc", "spice", "x11", "", True, 42,
        "waypipe-vsock " + "X" * 1000,
    ])
    def test_invalid_display_rejected(self, display):
        """Unrecognised/typed display values must be rejected."""
        res = validate_isolation({"tier": 5, "display": display})
        assert not ok(res), f"display={display!r} should be rejected"

    def test_valid_tier_4_accepted(self):
        assert ok(validate_isolation({"tier": 4}))

    def test_valid_tier_5_accepted(self):
        assert ok(validate_isolation({"tier": 5}))

    def test_valid_full_isolation_accepted(self):
        assert ok(validate_isolation({
            "tier": 5, "backend": "libvirt-qemu", "display": "waypipe-vsock"
        }))


# ---------------------------------------------------------------------------
# SECTION 2: validate_guest — hostile inputs
# ---------------------------------------------------------------------------

class TestGuestHostile:

    @pytest.mark.cheat_aware(
        protects="guest validator rejects unrecognised languages",
        severity="high",
        cheats=["allow any non-empty string as language"],
        consequence="arbitrary guest language string bypasses nix-ref requirements",
    )
    @pytest.mark.parametrize("bad_language", [
        "docker", "podman", "nixpkgs", "flake", "", None, True, 42, [],
        "nixos-module " + "x" * 1000,  # trailing garbage
        "NIXOS-MODULE",  # case mismatch
        "../../../etc/passwd",  # path traversal attempt
        "nixos-module\x00extra",  # NUL suffix
    ])
    def test_invalid_language_rejected(self, bad_language):
        """Unrecognised or hostile language values must be rejected."""
        guest = {"language": bad_language}
        if bad_language in ("nixos-module", "nixos-flake"):
            guest.update(flakeRef="r", lockRef="l", output="o")
        res = validate_guest(guest)
        assert not ok(res), f"language={bad_language!r} should be rejected"

    def test_guest_not_a_mapping_rejected(self):
        for bad in [None, [], "language=nixos-module", 42, True]:
            assert not ok(validate_guest(bad)), f"{bad!r} should be rejected"

    @pytest.mark.parametrize("lang", ["nixos-module", "nixos-flake"])
    def test_nix_guest_missing_required_refs_rejected(self, lang):
        """Nix guests without flakeRef/lockRef/output must be rejected."""
        res = validate_guest({"language": lang})
        assert not ok(res)
        for fld in ("flakeRef", "lockRef", "output"):
            assert any(fld in e for e in errs(res)), (
                f"expected error mentioning {fld!r}, got: {errs(res)}"
            )

    @pytest.mark.parametrize("field,bad_value", [
        ("flakeRef", 42),
        ("flakeRef", True),
        ("flakeRef", []),
        ("flakeRef", {}),
        ("lockRef", 42),
        ("lockRef", None),  # None for optional field that was omitted is fine;
                             # None when explicitly set means wrong type
        ("module", 42),
        ("output", []),
        ("system", True),
    ])
    def test_wrong_type_ref_fields_rejected(self, field, bad_value):
        """Wrong-typed reference fields must be rejected."""
        guest = {
            "language": "nixos-module",
            "flakeRef": "r",
            "lockRef": "l",
            "output": "o",
            field: bad_value,
        }
        res = validate_guest(guest)
        # None is only invalid when the field is required; skip if optional+None
        if bad_value is None and field in ("module", "system", "lockRef"):
            # module/system are optional; lockRef is required
            if field == "lockRef":
                assert not ok(res)
        else:
            assert not ok(res), (
                f"guest with {field}={bad_value!r} should be rejected"
            )

    def test_guest_valid_nixos_module_accepted(self):
        assert ok(validate_guest({
            "language": "nixos-module",
            "flakeRef": "git+file:///srv/x#y",
            "lockRef": "git+file:///srv/x?rev=abc",
            "output": "nixosConfigurations.y",
        }))

    def test_guest_valid_tumbleweed_accepted(self):
        assert ok(validate_guest({"language": "tumbleweed-script"}))


# ---------------------------------------------------------------------------
# SECTION 3: validate_exposed_services — hostile inputs
# ---------------------------------------------------------------------------

class TestExposedServicesHostile:

    @pytest.mark.cheat_aware(
        protects="exposed service entries with bad names are rejected",
        severity="critical",
        cheats=["only check that name is a string, skip regex validation"],
        consequence="arbitrary service names bypass allow-set, allow any vsock port",
    )
    @pytest.mark.parametrize("bad_name", [
        "Bad_Name",   # underscore forbidden in DNS-1123
        "BAD",        # uppercase forbidden
        "-start",     # starts with hyphen
        "end-",       # ends with hyphen
        "",            # empty
        None,
        True,
        42,
        "a" * 64,     # too long (DNS-1123 max 63 chars)
        "../../../etc/passwd",
        "valid\x00injected",  # NUL injection
        "name with spaces",
    ])
    def test_bad_service_name_rejected(self, bad_name):
        res = validate_exposed_services([{"name": bad_name, "vsockPort": 1}])
        assert not ok(res), f"name={bad_name!r} should be rejected"

    @pytest.mark.parametrize("bad_port", [
        0,          # below minimum (1)
        -1,         # negative
        0x100000000,  # above u32 max (0xFFFFFFFF)
        "7879",     # string
        7879.5,     # float
        True,       # bool subclass of int
        False,      # bool
        None,
        [],
        {},
    ])
    def test_bad_vsock_port_rejected(self, bad_port):
        res = validate_exposed_services([{"name": "waypipe", "vsockPort": bad_port}])
        assert not ok(res), f"vsockPort={bad_port!r} should be rejected"

    def test_services_not_a_list_rejected(self):
        for bad in [{"name": "x", "vsockPort": 1}, "x", 42, True]:
            assert not ok(validate_exposed_services(bad)), f"{bad!r} should be rejected"

    def test_service_entry_not_a_mapping_rejected(self):
        res = validate_exposed_services(["waypipe"])
        assert not ok(res)

    def test_duplicate_names_rejected(self):
        res = validate_exposed_services([
            {"name": "waypipe", "vsockPort": 1},
            {"name": "waypipe", "vsockPort": 2},
        ])
        assert not ok(res)

    def test_duplicate_ports_rejected(self):
        res = validate_exposed_services([
            {"name": "a", "vsockPort": 1},
            {"name": "b", "vsockPort": 1},
        ])
        assert not ok(res)

    def test_valid_boundary_port_accepted(self):
        # Min valid port
        assert ok(validate_exposed_services([{"name": "svc", "vsockPort": 1}]))
        # Max valid port (u32 max)
        assert ok(validate_exposed_services([{"name": "svc", "vsockPort": 0xFFFFFFFF}]))

    def test_none_services_accepted(self):
        assert ok(validate_exposed_services(None))

    def test_empty_list_accepted(self):
        assert ok(validate_exposed_services([]))


# ---------------------------------------------------------------------------
# SECTION 4: validate_publisher — hostile inputs
# ---------------------------------------------------------------------------

class TestPublisherHostile:

    @pytest.mark.parametrize("bad_mode", [
        "whole-vm", "per-vm", "batch", "", None, True, 42, [],
        "per-app " + "X" * 1000,
    ])
    def test_invalid_mode_rejected(self, bad_mode):
        res = validate_publisher({"mode": bad_mode, "command": "/bin/x"})
        if bad_mode is None:
            assert not ok(res)
        else:
            assert not ok(res), f"mode={bad_mode!r} should be rejected"

    @pytest.mark.parametrize("bad_command", [
        "", None, True, 42, [], {},
    ])
    def test_invalid_command_rejected(self, bad_command):
        res = validate_publisher({"mode": "per-app", "command": bad_command})
        assert not ok(res), f"command={bad_command!r} should be rejected"

    @pytest.mark.parametrize("bad_arbitrary", [
        "yes", "no", 0, 1, None,
    ])
    def test_non_bool_arbitrary_command_rejected(self, bad_arbitrary):
        if bad_arbitrary is None:
            # None means absent — acceptable (field is optional)
            assert ok(validate_publisher({
                "mode": "per-app", "command": "/bin/x"
            }))
        else:
            res = validate_publisher({
                "mode": "per-app",
                "command": "/bin/x",
                "arbitraryCommand": bad_arbitrary,
            })
            assert not ok(res), f"arbitraryCommand={bad_arbitrary!r} should be rejected"

    def test_publisher_not_a_mapping_rejected(self):
        for bad in [[], "mode=per-app", 42]:
            assert not ok(validate_publisher(bad)), f"{bad!r} should be rejected"


# ---------------------------------------------------------------------------
# SECTION 5: validate_vm_silo_spec — aggregate hostile inputs
# ---------------------------------------------------------------------------

class TestVmSiloSpecHostile:

    @pytest.mark.cheat_aware(
        protects="silo spec validator rejects every non-mapping input without crashing",
        severity="high",
        cheats=["isinstance check returns early for non-dict without validating internal fields"],
        consequence="hostile list/string/None input crashes the broker-side validation call",
    )
    @pytest.mark.parametrize("bad_input", [
        None, [], "spec: {}", 42, True, b"spec: {}",
    ])
    def test_non_mapping_rejected_without_crash(self, bad_input):
        res = validate_vm_silo_spec(bad_input)
        assert not ok(res)

    def test_spec_must_be_mapping_not_list(self):
        res = validate_vm_silo_spec({"spec": ["isolation", "guest"]})
        assert not ok(res)

    def test_non_vm_tier_with_guest_block_rejected(self):
        """A tier-2 silo must not carry a guest block."""
        m = _good_spec()
        m["spec"]["isolation"]["tier"] = 2
        res = validate_vm_silo_spec(m)
        assert not ok(res)

    def test_vm_tier_without_guest_block_rejected(self):
        m = _good_spec()
        del m["spec"]["guest"]
        res = validate_vm_silo_spec(m)
        assert not ok(res)

    def test_valid_tier4_spec_accepted(self):
        m = _good_spec()
        m["spec"]["isolation"]["tier"] = 4
        assert ok(validate_vm_silo_spec(m))

    def test_or_raise_raises_on_bad_input(self):
        with pytest.raises(MetadataSchemaError):
            validate_vm_silo_spec_or_raise(None)
        with pytest.raises(MetadataSchemaError):
            validate_vm_silo_spec_or_raise({"spec": {}})

    def test_or_raise_passes_on_good_input(self):
        validate_vm_silo_spec_or_raise(_good_spec())


# ---------------------------------------------------------------------------
# SECTION 6: validate_image_manifest — hostile inputs
# ---------------------------------------------------------------------------

class TestImageManifestHostile:

    @pytest.mark.cheat_aware(
        protects="image manifest validator fails closed on non-mapping input",
        severity="high",
        cheats=["isinstance check returns True for dict subclasses without validation"],
        consequence="malformed manifest activates an unverified VM image",
    )
    @pytest.mark.parametrize("bad_input", [
        None, [], "kind: Image", 42, True,
    ])
    def test_non_mapping_rejected(self, bad_input):
        res = validate_image_manifest(bad_input)
        assert not ok(res)

    def test_wrong_kind_rejected(self):
        m = _good_image()
        m["kind"] = "Silo"
        assert not ok(validate_image_manifest(m))

    def test_missing_kind_rejected(self):
        m = _good_image()
        del m["kind"]
        assert not ok(validate_image_manifest(m))

    def test_missing_api_version_rejected(self):
        m = _good_image()
        del m["apiVersion"]
        assert not ok(validate_image_manifest(m))

    @pytest.mark.parametrize("bad_digest", [
        "not-a-digest",
        "sha256:",              # empty body
        "sha256:deadbeef",     # too short (8 hex, need 64)
        "sha256:" + "a" * 63, # one short
        "sha256:" + "a" * 65, # one over
        "sha256:" + "g" * 64, # invalid hex chars
        "sha256:" + "A" * 128,  # sha256 must be 64 chars not 128
        ":abc",                 # missing algorithm
        "abc",                  # no colon separator
        "",
        None,
        True,
        42,
    ])
    def test_bad_digest_rejected(self, bad_digest):
        m = _good_image()
        m["output"]["digest"] = bad_digest
        res = validate_image_manifest(m)
        assert not ok(res), f"digest={bad_digest!r} should be rejected"

    def test_sha512_correct_length_accepted(self):
        m = _good_image()
        m["output"]["digest"] = "sha512:" + "ab" * 64  # 128 hex chars
        assert ok(validate_image_manifest(m))

    def test_sha256_exact_length_accepted(self):
        m = _good_image()
        m["output"]["digest"] = "sha256:" + "0" * 64
        assert ok(validate_image_manifest(m))

    def test_empty_health_checks_rejected(self):
        m = _good_image()
        m["healthChecks"] = []
        assert not ok(validate_image_manifest(m))

    def test_null_health_checks_rejected(self):
        m = _good_image()
        m["healthChecks"] = None
        assert not ok(validate_image_manifest(m))

    def test_health_check_not_mapping_rejected(self):
        m = _good_image()
        m["healthChecks"] = ["publisher-up"]  # should be dicts
        assert not ok(validate_image_manifest(m))

    def test_health_check_duplicate_name_rejected(self):
        m = _good_image()
        m["healthChecks"] = [{"name": "x"}, {"name": "x"}]
        assert not ok(validate_image_manifest(m))

    def test_build_command_empty_list_rejected(self):
        m = _good_image()
        m["build"]["command"] = []
        assert not ok(validate_image_manifest(m))

    def test_build_command_list_with_non_string_rejected(self):
        m = _good_image()
        m["build"]["command"] = ["nix", 42, "build"]
        assert not ok(validate_image_manifest(m))

    def test_build_sandbox_non_bool_rejected(self):
        m = _good_image()
        m["build"]["sandboxed"] = "yes"
        assert not ok(validate_image_manifest(m))

    def test_tumbleweed_packages_bare_string_rejected(self):
        m = _good_image()
        m["definition"] = {
            "language": "tumbleweed-script",
            "packages": "cups",  # must be a list
            "sourceRefs": ["obs://x"],
        }
        assert not ok(validate_image_manifest(m))

    def test_tumbleweed_packages_non_string_entries_rejected(self):
        m = _good_image()
        m["definition"] = {
            "language": "tumbleweed-script",
            "packages": ["cups", 42],  # 42 is not a string
            "sourceRefs": ["obs://x"],
        }
        assert not ok(validate_image_manifest(m))

    def test_tumbleweed_missing_packages_rejected(self):
        m = _good_image()
        m["definition"] = {
            "language": "tumbleweed-script",
            "sourceRefs": ["obs://x"],
        }
        assert not ok(validate_image_manifest(m))

    def test_nix_missing_lock_ref_rejected(self):
        m = _good_image()
        del m["definition"]["lockRef"]
        assert not ok(validate_image_manifest(m))

    def test_or_raise_on_bad_image(self):
        with pytest.raises(MetadataSchemaError):
            validate_image_manifest_or_raise({"kind": "Image"})

    def test_or_raise_passes_on_good_image(self):
        validate_image_manifest_or_raise(_good_image())


# ---------------------------------------------------------------------------
# SECTION 7: tumbleweed_image_manifest emitter — hostile caller inputs
# ---------------------------------------------------------------------------

class TestTumbleweedEmitterHostile:

    @pytest.mark.cheat_aware(
        protects="tumbleweed_image_manifest rejects bare strings for list arguments",
        severity="high",
        cheats=["wrap string in list() which char-splits it"],
        consequence="package 'firefox-esr' becomes ['f','i','r','e','f','o','x','-','e','s','r'] in the lineage record",
    )
    @pytest.mark.parametrize("field,bad_value", [
        ("packages", "cups"),
        ("source_refs", "obs://x"),
        ("health_checks", {"name": "x"}),
    ])
    def test_string_instead_of_list_raises(self, field, bad_value):
        kwargs = dict(
            name="vm",
            packages=["cups"],
            source_refs=["obs://x"],
            build_command="build.sh",
            builder="root@host",
            output_path="/srv/x.qcow2",
            output_digest="sha256:" + "ab" * 32,
            health_checks=[{"name": "x"}],
        )
        kwargs[field] = bad_value
        with pytest.raises(MetadataSchemaError):
            tumbleweed_image_manifest(**kwargs)

    def test_bad_digest_raises(self):
        with pytest.raises(MetadataSchemaError):
            tumbleweed_image_manifest(
                name="vm",
                packages=["cups"],
                source_refs=["obs://x"],
                build_command="build.sh",
                builder="root@host",
                output_path="/srv/x.qcow2",
                output_digest="not-a-digest",
                health_checks=[{"name": "x"}],
            )

    def test_valid_emitter_accepted(self):
        m = tumbleweed_image_manifest(
            name="print-vm",
            packages=["cups", "waypipe"],
            source_refs=["obs://qdistro/print-vm"],
            build_command=["build.sh"],
            builder="root@host",
            output_path="/srv/x.qcow2",
            output_digest="sha256:" + "ab" * 32,
            health_checks=[{"name": "cups-up"}],
        )
        assert ok(validate_image_manifest(m))
        assert m["definition"]["language"] == "tumbleweed-script"


# ---------------------------------------------------------------------------
# SECTION 8: check_guest_services_match_manifest — fail-closed hostile inputs
# ---------------------------------------------------------------------------

class TestActivationGuardHostile:

    @pytest.mark.cheat_aware(
        protects="activation guard blocks when spec_guest is not a valid mapping",
        severity="critical",
        cheats=["silently allow when spec_guest is falsy"],
        consequence="silo activates without any service manifest check — every service is implicitly allowed",
    )
    @pytest.mark.parametrize("bad_spec_guest", [
        None, [], "language=tumbleweed-script", 42, True, b"{}",
    ])
    def test_invalid_spec_guest_blocks_activation(self, bad_spec_guest):
        res = check_guest_services_match_manifest(bad_spec_guest, [])
        assert not ok(res), f"spec_guest={bad_spec_guest!r} should block activation"
        assert any("activation blocked" in e for e in errs(res))

    @pytest.mark.cheat_aware(
        protects="activation guard blocks when guest_exposed is None (no discovery data)",
        severity="critical",
        cheats=["treat None as empty list"],
        consequence="silo activates without knowing which services the guest actually exposes",
    )
    def test_none_guest_exposed_blocks_activation(self):
        spec_guest = {"language": "tumbleweed-script"}
        res = check_guest_services_match_manifest(spec_guest, None)
        assert not ok(res)
        assert any("unknown" in e for e in errs(res))

    @pytest.mark.cheat_aware(
        protects="activation guard blocks when a guest exposes an undeclared service",
        severity="critical",
        cheats=["skip check when allow-set is empty"],
        consequence="any guest can expose arbitrary vsock services when the manifest declares none",
    )
    def test_undeclared_service_blocks_activation(self):
        spec_guest = {"language": "tumbleweed-script"}  # no exposedServices
        res = check_guest_services_match_manifest(
            spec_guest, [{"name": "sshd", "vsockPort": 22}]
        )
        assert not ok(res)
        assert any("not declared" in e for e in errs(res))

    @pytest.mark.parametrize("bad_entry", [
        42, None, True, 3.14,
    ])
    def test_bad_exposed_entry_type_blocks(self, bad_entry):
        spec_guest = _good_spec()["spec"]["guest"]
        res = check_guest_services_match_manifest(spec_guest, [bad_entry])
        assert not ok(res)

    @pytest.mark.parametrize("bad_port", [
        True, False, "7879", 7879.5, -1, 0, 0x100000001,
    ])
    def test_bad_exposed_port_type_blocks(self, bad_port):
        spec_guest = _good_spec()["spec"]["guest"]
        res = check_guest_services_match_manifest(
            spec_guest, [{"name": "waypipe", "vsockPort": bad_port}]
        )
        assert not ok(res)

    def test_port_mismatch_blocks(self):
        spec_guest = _good_spec()["spec"]["guest"]  # waypipe @ 7879
        res = check_guest_services_match_manifest(
            spec_guest, [{"name": "waypipe", "vsockPort": 9999}]
        )
        assert not ok(res)

    def test_bare_name_without_port_blocks_when_manifest_pins(self):
        """Manifest pins port 7879; a bare-name entry cannot confirm it."""
        spec_guest = _good_spec()["spec"]["guest"]
        res = check_guest_services_match_manifest(spec_guest, ["waypipe"])
        assert not ok(res)
        assert any("omits a valid port" in e for e in errs(res))

    def test_invalid_spec_guest_block_structurally(self):
        """Structurally invalid spec.guest (missing required Nix refs) blocks."""
        spec_guest = {"language": "nixos-module"}  # missing flakeRef/lockRef/output
        res = check_guest_services_match_manifest(spec_guest, [])
        assert not ok(res)
        assert any("structurally invalid" in e for e in errs(res))

    def test_duplicate_allowset_name_blocks(self):
        spec_guest = {
            "language": "tumbleweed-script",
            "exposedServices": [
                {"name": "waypipe", "vsockPort": 1},
                {"name": "waypipe", "vsockPort": 2},
            ],
        }
        res = check_guest_services_match_manifest(spec_guest, [{"name": "waypipe"}])
        assert not ok(res)

    def test_valid_match_accepted(self):
        spec_guest = _good_spec()["spec"]["guest"]
        assert ok(check_guest_services_match_manifest(
            spec_guest, [{"name": "waypipe", "vsockPort": 7879}]
        ))

    def test_empty_exposed_empty_allowset_accepted(self):
        spec_guest = {"language": "tumbleweed-script"}
        assert ok(check_guest_services_match_manifest(spec_guest, []))

    def test_or_raise_on_mismatch(self):
        spec_guest = _good_spec()["spec"]["guest"]
        with pytest.raises(MetadataSchemaError):
            check_guest_services_match_manifest_or_raise(
                spec_guest, [{"name": "sshd", "vsockPort": 22}]
            )

    def test_or_raise_on_valid_match(self):
        spec_guest = _good_spec()["spec"]["guest"]
        check_guest_services_match_manifest_or_raise(
            spec_guest, [{"name": "waypipe", "vsockPort": 7879}]
        )


# ---------------------------------------------------------------------------
# SECTION 9: Digest validation edge cases
# ---------------------------------------------------------------------------

class TestDigestEdgeCases:

    @pytest.mark.parametrize("digest,expected_ok", [
        ("sha256:" + "a" * 64, True),
        ("sha384:" + "b" * 96, True),
        ("sha512:" + "c" * 128, True),
        ("blake2b:" + "d" * 128, True),
        ("blake2s:" + "e" * 64, True),
        # Unknown algorithm: minimum length is 32 hex chars
        ("unknown-algo:" + "f" * 32, True),
        ("unknown-algo:" + "f" * 31, False),  # below minimum
        # Wrong lengths for known algorithms
        ("sha256:" + "a" * 63, False),
        ("sha256:" + "a" * 65, False),
        ("sha384:" + "b" * 95, False),
        ("sha512:" + "c" * 127, False),
        ("blake2b:" + "d" * 127, False),
        # Non-hex body
        ("sha256:" + "g" * 64, False),
        ("sha256:" + "G" * 64, False),  # uppercase G is not hex
        # Structural issues
        ("sha256::", False),
        (":abc" * 16, False),
        ("", False),
        ("sha256", False),
    ])
    def test_digest_validation(self, digest, expected_ok):
        m = _good_image()
        m["output"]["digest"] = digest
        res = validate_image_manifest(m)
        assert ok(res) == expected_ok, (
            f"digest={digest!r}: expected ok={expected_ok} but got ok={ok(res)}, "
            f"errors={errs(res)}"
        )
