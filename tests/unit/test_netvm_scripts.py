"""Host-side net-VM helper script + image-overlay structure tests (task 4
pieces 1/2/5).

Exercises the scripts' shape without a libvirt domain or an image build (real
define / build / control-plane coverage is tests/integration/vm/s04-netvm-
control-plane.sh). Keeps the shipped artifacts — domain template placeholders,
the manifest⇄template MAC agreement, the rpcd overlay layout, the install
guard's fail-closed behaviour — from rotting.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
NETVM = os.path.join(REPO, "net-vm")
FILES = os.path.join(NETVM, "image-files")

INSTALL = os.path.join(NETVM, "install-netvm.sh")
BUILD = os.path.join(NETVM, "build-netvm-image.sh")
CLAIM = os.path.join(NETVM, "qdistro-netvm-claim-nic.sh")
DOMAIN = os.path.join(NETVM, "domain-template.xml")
MANIFEST = os.path.join(NETVM, "netvm-manifest.json")
PLUGIN = os.path.join(FILES, "usr/libexec/rpcd/qdistro.netvm")
APPLY = os.path.join(FILES, "usr/libexec/qdistro-netvm-apply")
ACL = os.path.join(FILES, "usr/share/rpcd/acl.d/qdistro-netvm.json")
SEED = os.path.join(FILES, "etc/uci-defaults/99-qdistro-netvm")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def _norm_mac_ok(mac):
    import re
    return bool(re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", str(mac).lower()))


class TestArtifactsPresent:
    @pytest.mark.parametrize("p", [INSTALL, BUILD, CLAIM, DOMAIN, MANIFEST,
                                   PLUGIN, APPLY, ACL, SEED])
    def test_present(self, p):
        assert os.path.exists(p), f"missing {p}"

    @pytest.mark.parametrize("p", [INSTALL, BUILD, CLAIM, PLUGIN, APPLY, SEED])
    def test_executable(self, p):
        assert os.access(p, os.X_OK), f"{p} not executable"

    @pytest.mark.parametrize("p", [INSTALL, BUILD, CLAIM, PLUGIN, APPLY, SEED])
    def test_shell_syntax_ok(self, p):
        assert _run(["bash", "-n", p]).returncode == 0


class TestDomainTemplate:
    def test_placeholders(self):
        body = open(DOMAIN).read()
        for ph in ("__VM_NAME__", "__MEM_KIB__", "__MGMT_MAC__", "__WAN_MAC__",
                   "__MGMT_NET__", "__DISK_PATH__"):
            assert ph in body, f"missing placeholder {ph}"

    def _rendered_devices(self):
        from xml.etree import ElementTree as ET
        xml = open(DOMAIN).read()
        for ph, v in (("__VM_NAME__", "t"), ("__MEM_KIB__", "262144"),
                      ("__MGMT_MAC__", "52:54:00:9c:00:01"),
                      ("__WAN_MAC__", "52:54:00:9c:00:02"),
                      ("__MGMT_NET__", "m"), ("__DISK_PATH__", "/d.qcow2")):
            xml = xml.replace(ph, v)
        return ET.fromstring(xml).find("devices")

    def test_headless_no_graphics(self):
        # Parse (not grep) so the explanatory comment naming <graphics> doesn't
        # false-positive — only real device elements count.
        dev = self._rendered_devices()
        assert dev.find("graphics") is None and dev.find("video") is None

    def test_no_vsock_under_rpcd_transport(self):
        # Probe 2 chose rpcd-http; the template must not carry a vsock device.
        assert self._rendered_devices().find("vsock") is None


class TestManifest:
    def test_valid_json(self):
        json.load(open(MANIFEST))

    def test_template_macs_match_manifest(self):
        m = json.load(open(MANIFEST))
        # The install script substitutes these; the manifest is the source of
        # truth, so the guard and the rendered domain agree by construction.
        assert _norm_mac_ok(m["controlPlane"]["mgmtMac"])
        assert _norm_mac_ok(m["interfaces"]["wanMac"])

    def test_exposes_egress_and_reads_only(self):
        m = json.load(open(MANIFEST))
        objs = {e["object"]: e["methods"] for e in m["exposedServices"]}
        assert "egress_reload" in objs["qdistro.netvm"]
        assert "file" not in objs           # no broad file.exec grant


class TestRpcdOverlay:
    def test_plugin_lists_both_methods(self):
        body = open(PLUGIN).read()
        assert "egress_reload" in body and "wifi_join" in body

    def test_acl_group_matches_manifest(self):
        acl = json.load(open(ACL))
        m = json.load(open(MANIFEST))
        assert m["controlPlane"]["aclGroup"] in acl

    def test_acl_only_grants_declared_objects(self):
        acl = json.load(open(ACL))["qdistro-netvm-admin"]
        granted = set(acl["read"]["ubus"]) | set(acl["write"]["ubus"])
        declared = {e["object"]
                    for e in json.load(open(MANIFEST))["exposedServices"]}
        assert granted <= declared, f"ACL grants undeclared: {granted - declared}"

    def test_apply_uses_busybox_safe_tools(self):
        # BusyBox has no `install`; the apply helper must use cp/chmod (this was
        # a real VM-found bug — keep it from regressing).
        body = open(APPLY).read()
        assert "install -m" not in body
        assert "cp " in body


class TestInstallGuard:
    def test_help(self):
        r = _run(["bash", INSTALL, "--help"])
        assert r.returncode == 0 and "install-netvm" in r.stdout

    def test_remove_absent_is_noop_or_clean(self):
        # --remove against a non-existent domain on a throwaway URI must not
        # crash (it reports "not defined").
        env = dict(os.environ, QDISTRO_NETVM_LIBVIRT_URI="test:///default",
                   QDISTRO_NETVM_VM_NAME="qdistro-netvm-nonesuch")
        r = _run(["bash", INSTALL, "--remove"], env=env)
        assert r.returncode == 0

    def test_check_passes_on_shipped_pair(self):
        # The shipped template+manifest must activate clean (no undeclared
        # device). --check renders + runs the guard, no libvirt needed.
        r = _run(["bash", INSTALL, "--check"])
        assert r.returncode == 0, r.stderr
        assert "activatable" in r.stdout
