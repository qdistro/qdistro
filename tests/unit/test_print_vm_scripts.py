"""spec/20 Phase-9 §step 2 — host-side print-VM helper script tests.

Exercises shell-script behaviour without an actual libvirt domain:
  - install-print-vm.sh: --remove on absent domain is a no-op,
    --help prints, missing virsh fails fast.
  - qdistro-print-attach-usb.sh: arg parsing, --help, missing virsh.
  - qdistro-print-detach-usb.sh: same shape.
  - build-print-image.sh: --help, refuses to run as non-root.

Real domain define / image build coverage lives in the in-VM phase8
bats — pytest doesn't have libvirt running on the host runner.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap

import pytest

REPO = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", ".."))
PRINT_VM_DIR = os.path.join(REPO, "print-vm")

INSTALL = os.path.join(PRINT_VM_DIR, "install-print-vm.sh")
ATTACH = os.path.join(PRINT_VM_DIR, "qdistro-print-attach-usb.sh")
DETACH = os.path.join(PRINT_VM_DIR, "qdistro-print-detach-usb.sh")
BUILD = os.path.join(PRINT_VM_DIR, "build-print-image.sh")
DOMAIN = os.path.join(PRINT_VM_DIR, "domain-template.xml")
USB_MANIFEST = os.path.join(PRINT_VM_DIR, "printvm-manifest.json")
JOBS = os.path.join(PRINT_VM_DIR, "qdistro-print-jobs")


def _run(cmd, env=None, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          env=env, **kw)


# -- domain-template ----------------------------------------------------

class TestDomainTemplate:
    def test_template_present(self):
        assert os.path.exists(DOMAIN)
        with open(DOMAIN) as f:
            body = f.read()
        for placeholder in ("__VM_NAME__", "__MAC__", "__MEM_KIB__",
                            "__CID__", "__DISK_PATH__"):
            assert placeholder in body, f"missing placeholder {placeholder}"

    def test_template_uses_qemu_xhci_for_usb(self):
        with open(DOMAIN) as f:
            assert "qemu-xhci" in f.read(), \
                "USB hot-plug needs qemu-xhci controller"

    def test_template_has_vsock(self):
        with open(DOMAIN) as f:
            body = f.read()
        assert "<vsock" in body and "__CID__" in body

    def test_usb_manifest_defaults_deny(self):
        import json
        with open(USB_MANIFEST, encoding="utf-8") as f:
            obj = json.load(f)
        assert obj["name"] == "qdistro-print"
        assert obj["usbHostdevAllow"] == []


# -- install-print-vm.sh -----------------------------------------------

class TestInstallScript:
    def test_help(self):
        out = _run(["bash", INSTALL, "--help"])
        assert out.returncode == 0
        assert "qdistro-print" in out.stdout or "install" in out.stdout.lower()

    def test_unknown_arg_rejected(self):
        out = _run(["bash", INSTALL, "--bogus"])
        assert out.returncode != 0

    def test_missing_virsh_fails_fast(self, tmp_path, monkeypatch):
        # Sandbox PATH: only the dirs holding bash + grep + sed. We
        # symlink those in so the script can run, but virsh is absent
        # so the script bails on the `command -v virsh` check.
        sand = tmp_path / "sand"
        sand.mkdir()
        for tool in ("bash", "grep", "sed", "cat", "ls", "rm"):
            real = shutil.which(tool)
            if real:
                os.symlink(real, str(sand / tool))
        env = {"PATH": str(sand), "HOME": os.environ.get("HOME", "/tmp")}
        out = _run(["bash", INSTALL], env=env)
        assert out.returncode == 1
        assert "virsh" in out.stderr

    def test_remove_when_absent_no_op(self):
        if shutil.which("virsh") is None:
            pytest.skip("virsh not installed on host")
        env = dict(os.environ)
        env["QDISTRO_PRINT_VM_NAME"] = "nonexistent-print-domain-260430"
        env["QDISTRO_PRINT_LIBVIRT_URI"] = "qemu:///session"
        out = _run(["bash", INSTALL, "--remove"], env=env)
        assert out.returncode == 0
        assert "not defined" in out.stdout.lower() \
            or "undefined" in out.stdout.lower()


# -- attach / detach ---------------------------------------------------

class TestAttachDetach:
    def _attach_sandbox(self, tmp_path, *, pkcheck_rc=0,
                        manifest='{"usbHostdevAllow":["0411:1234"]}'):
        sand = tmp_path / "sand"
        sand.mkdir()
        for tool in ("bash", "grep", "sed", "cat", "rm", "mktemp",
                     "dirname", "head", "python3"):
            real = shutil.which(tool)
            if real:
                os.symlink(real, str(sand / tool))
        attach = tmp_path / "qdistro-print-attach-usb.sh"
        shutil.copyfile(ATTACH, attach)
        attach.chmod(0o755)
        (tmp_path / "printvm-manifest.json").write_text(manifest)
        (sand / "pkcheck").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            printf '%s\\n' "$*" > {tmp_path}/pkcheck.args
            exit {pkcheck_rc}
        """))
        (sand / "pkcheck").chmod(0o755)
        (sand / "virsh").write_text(textwrap.dedent(f"""\
            #!/bin/bash
            if [ "$3" = "list" ]; then
                echo qdistro-print
                exit 0
            fi
            if [ "$3" = "attach-device" ]; then
                echo "$4" > {tmp_path}/attached.vm
                exit 0
            fi
            exit 99
        """))
        (sand / "virsh").chmod(0o755)
        env = {"PATH": str(sand), "HOME": os.environ.get("HOME", "/tmp")}
        return attach, env

    def test_attach_help(self):
        out = _run(["bash", ATTACH, "--help"])
        assert out.returncode == 0
        assert "vendor-product" in out.stdout
        assert "--vm" not in out.stdout

    def test_detach_help(self):
        out = _run(["bash", DETACH, "--help"])
        assert out.returncode == 0
        assert "vendor-product" in out.stdout
        assert "--vm" not in out.stdout

    def test_attach_requires_device_spec(self):
        out = _run(["bash", ATTACH])
        assert out.returncode == 2
        assert "vendor-product" in (out.stdout + out.stderr)

    def test_detach_requires_device_spec(self):
        out = _run(["bash", DETACH])
        assert out.returncode == 2

    def test_attach_rejects_malformed_vendor_product(self):
        out = _run(["bash", ATTACH, "--vendor-product", "0411"])
        assert out.returncode == 2

    def test_detach_rejects_malformed_vendor_product(self):
        out = _run(["bash", DETACH, "--vendor-product", "0411"])
        assert out.returncode == 2

    def test_attach_missing_virsh_errors(self, tmp_path):
        sand = tmp_path / "sand"
        sand.mkdir()
        for tool in ("bash", "grep", "sed", "cat", "od", "tr", "rm",
                     "mktemp", "date"):
            real = shutil.which(tool)
            if real:
                os.symlink(real, str(sand / tool))
        env = {"PATH": str(sand),
               "HOME": os.environ.get("HOME", "/tmp"),
               "QDISTRO_PRINT_USB_NO_POLKIT": "1"}
        out = _run(["bash", ATTACH, "--vendor-product", "0411:1234"],
                   env=env)
        assert out.returncode == 1
        assert "virsh" in out.stderr

    def test_attach_missing_pkcheck_fails_closed(self, tmp_path):
        sand = tmp_path / "sand"
        sand.mkdir()
        for tool in ("bash",):
            real = shutil.which(tool)
            if real:
                os.symlink(real, str(sand / tool))
        (sand / "virsh").write_text("#!/bin/bash\nexit 0\n")
        (sand / "virsh").chmod(0o755)
        env = {"PATH": str(sand), "HOME": os.environ.get("HOME", "/tmp")}
        out = _run(["bash", ATTACH, "--vendor-product", "0411:1234"], env=env)
        assert out.returncode == 5
        assert "pkcheck" in out.stderr
        assert "refusing" in out.stderr

    def test_attach_ignores_polkit_env_bypass(self, tmp_path):
        attach, env = self._attach_sandbox(tmp_path, pkcheck_rc=42)
        env["QDISTRO_PRINT_USB_NO_POLKIT"] = "1"
        out = _run(["bash", str(attach), "--vendor-product", "0411:1234"], env=env)
        assert out.returncode == 5
        assert "polkit denied" in out.stderr
        assert not (tmp_path / "attached.vm").exists()

    def test_attach_pkcheck_process_uses_real_pid_only(self, tmp_path):
        attach, env = self._attach_sandbox(tmp_path)
        out = _run(["bash", str(attach), "--vendor-product", "0411:1234"], env=env)
        assert out.returncode == 0, out.stderr
        args = (tmp_path / "pkcheck.args").read_text()
        assert "--process" in args
        process_arg = args.split("--process ", 1)[1].split()[0]
        assert "," not in process_arg

    def test_attach_target_vm_is_pinned(self, tmp_path):
        attach, env = self._attach_sandbox(tmp_path)
        env["QDISTRO_PRINT_VM_NAME"] = "evil-vm"
        out = _run(["bash", str(attach), "--vendor-product", "0411:1234"], env=env)
        assert out.returncode == 0, out.stderr
        assert (tmp_path / "attached.vm").read_text().strip() == "qdistro-print"

    def test_attach_rejects_vm_argument(self, tmp_path):
        attach, env = self._attach_sandbox(tmp_path)
        out = _run(["bash", str(attach), "--vendor-product", "0411:1234",
                    "--vm", "evil-vm"], env=env)
        assert out.returncode == 2
        assert "pinned" in out.stderr

    def test_attach_requires_usb_allowlist_match(self, tmp_path):
        attach, env = self._attach_sandbox(tmp_path, manifest='{"usbHostdevAllow":[]}')
        out = _run(["bash", str(attach), "--vendor-product", "0411:1234"], env=env)
        assert out.returncode == 7
        assert "not in print VM usbHostdevAllow" in out.stderr
        assert not (tmp_path / "attached.vm").exists()


# -- build-print-image -------------------------------------------------

class TestBuildScript:
    def test_help(self):
        out = _run(["bash", BUILD, "--help"])
        assert out.returncode == 0
        assert "Tumbleweed" in out.stdout or "qcow" in out.stdout.lower()

    def test_unknown_arg_rejected(self):
        out = _run(["bash", BUILD, "--bogus"])
        assert out.returncode == 2

    def test_refuses_non_root(self):
        # The host pytest runs as a normal user; the script bails.
        if os.geteuid() == 0:
            pytest.skip("test invariant: pytest runs as non-root")
        out = _run(["bash", BUILD])
        assert out.returncode == 2
        assert "root" in out.stderr.lower()


# -- priority #6: build-script encodes the cupsd cap shape --------------

class TestBuildScriptCaps:
    """Pin the job-size + queue caps in the embedded cupsd.conf
    heredoc so a future refactor doesn't silently regress the limits.
    The heredoc is part of build-print-image.sh; we read the file and
    grep for the numeric tokens. Real-VM coverage lives in the in-VM
    phase8 bats — these checks pin the source-of-truth.
    """

    def _build_text(self):
        with open(BUILD, "r", encoding="utf-8") as f:
            return f.read()

    def test_max_jobs_set(self):
        text = self._build_text()
        assert "MaxJobs" in text
        assert "MaxJobs               500" in text

    def test_max_jobs_per_user_set(self):
        text = self._build_text()
        assert "MaxJobsPerUser         50" in text

    def test_max_jobs_per_printer_set(self):
        text = self._build_text()
        assert "MaxJobsPerPrinter     200" in text

    def test_max_request_size_64mb(self):
        text = self._build_text()
        assert "MaxRequestSize  67108864" in text

    def test_preserve_job_files_disabled(self):
        text = self._build_text()
        assert "PreserveJobFiles        No" in text

    def test_preserve_job_history_enabled(self):
        text = self._build_text()
        assert "PreserveJobHistory     Yes" in text


class TestSetPageLimitHelper:
    """Pin the qdistro-print-set-page-limit helper that build-print-
    image.sh installs into /usr/local/bin/ on the print-base image.
    """

    def _build_text(self):
        with open(BUILD, "r", encoding="utf-8") as f:
            return f.read()

    def test_helper_emitted_in_build_script(self):
        text = self._build_text()
        assert "qdistro-print-set-page-limit" in text
        assert "job-page-limit-default" in text

    def test_helper_validates_int(self):
        text = self._build_text()
        assert "non-negative integer" in text

    def test_helper_check_queue_exists(self):
        text = self._build_text()
        assert "lpstat -p" in text

    def test_helper_copy_in(self):
        text = self._build_text()
        assert "qdistro-print-set-page-limit:/usr/local/bin/" in text


class TestJobsCli:
    """qdistro-print-jobs — host-side qga wrapper for the in-VM
    qdistro-print-job-control. task(109).
    """

    def test_help_prints_subcommands(self):
        out = _run(["bash", JOBS, "--help"])
        assert out.returncode == 0
        # `list`/`cancel`/`purge` must appear in the help.
        text = out.stdout
        assert "list" in text
        assert "cancel" in text
        assert "purge" in text

    def test_no_args_errors(self):
        out = _run(["bash", JOBS])
        assert out.returncode == 2

    def test_unknown_subcommand_rejected(self):
        out = _run(["bash", JOBS, "bogus"])
        assert out.returncode == 2

    def test_cancel_requires_jobid(self):
        out = _run(["bash", JOBS, "cancel"])
        assert out.returncode == 2
        assert "jobid" in (out.stdout + out.stderr).lower()

    def test_cancel_rejects_non_ascii_jobid(self):
        # CUPS job IDs are <queue>-<int>; we restrict charset so a
        # caller-supplied jobid can't break out of the JSON envelope
        # passed through `virsh qemu-agent-command`.
        out = _run(["bash", JOBS, "cancel", "$(rm -rf /)"])
        assert out.returncode == 2

    def test_uses_default_vm_name(self):
        # Without QDISTRO_PRINT_VM the wrapper targets the default
        # `qdistro-print` domain. Pin that default so callers can rely
        # on it.
        with open(JOBS) as f:
            body = f.read()
        assert "qdistro-print" in body
        assert "QDISTRO_PRINT_VM" in body


class TestCupsBrowsedDefault:
    """Priority #5 default-deny cups-browsed.conf shipped by the build
    script; pin the shape so neither cap nor allowlist regresses."""

    def _build_text(self):
        with open(BUILD, "r", encoding="utf-8") as f:
            return f.read()

    def test_default_deny_in_build_script(self):
        text = self._build_text()
        assert "BrowseAllow none" in text

    def test_browsed_conf_copy_in(self):
        text = self._build_text()
        assert "etc/cups-browsed.conf:/etc/" in text
