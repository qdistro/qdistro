"""Backend cleanup outcomes: OS failures retain ownership and never fake success."""
import os
import shutil
import subprocess
import sys

import pytest

import secret_delivery as sd


def test_failed_revoke_wipes_and_retry_is_idempotent():
    class Broken(sd.DeliveryHandle):
        attempts = 0

        def _revoke(self):
            self.attempts += 1
            if self.attempts == 1:
                raise sd.DeliveryError("injected failure")

    secret = sd.SecretValue(b"dummy")
    handle = Broken(secret)
    with pytest.raises(sd.DeliveryError):
        handle.scrub()
    assert secret.wiped and not handle.scrubbed
    assert handle.metadata()["buffer_wiped"]
    assert handle.metadata()["cleanup_error"] == "revocation failed"
    handle.scrub()
    handle.scrub()
    assert handle.scrubbed and handle.attempts == 2
    assert handle.metadata()["cleanup_error"] is None


@pytest.mark.integration
def test_group_signal_denial_retains_owned_child_for_retry(monkeypatch):
    handle = sd.EnvDelivery(sd.SecretValue(b"dummy"), var="SECRET",
                            command=[sys.executable, "-c", "pass"])
    handle.deliver()
    proc = handle._process
    with monkeypatch.context() as patch:
        def denied(*args):
            raise PermissionError("signal denied")
        patch.setattr(sd.os, "killpg", denied)
        with pytest.raises(PermissionError):
            handle.scrub()
    assert handle._process is proc and proc.returncode is None
    assert handle._secret.wiped and not handle.scrubbed
    handle.scrub()
    assert handle._process is None and handle.scrubbed
    with pytest.raises(ChildProcessError):
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)


@pytest.mark.integration
def test_released_child_identity_never_signals_numeric_group(monkeypatch):
    handle = sd.EnvDelivery(sd.SecretValue(b"dummy"), var="SECRET",
                            command=[sys.executable, "-c", "pass"])
    handle.deliver()
    handle._process.wait()  # simulate an external consumer reaping the leader
    signals = []
    monkeypatch.setattr(sd.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(sd.DeliveryError, match="identity was already released"):
        handle.scrub()
    assert signals == []
    assert not handle.scrubbed and handle._secret.wiped


def test_failed_fd_close_never_retries_reused_descriptor():
    handle = sd.FdPassDelivery(sd.SecretValue(b"dummy"))
    handle.deliver()
    fd = handle.read_fd
    os.close(fd)
    with pytest.raises(OSError):
        handle.scrub()
    assert handle.read_fd is None and not handle.scrubbed
    replacement = os.open("/dev/null", os.O_RDONLY)
    try:
        assert replacement == fd
        with pytest.raises(sd.DeliveryError, match="outcome is unknown"):
            handle.scrub()
        os.fstat(replacement)
        assert not handle.scrubbed and handle._secret.wiped
    finally:
        os.close(replacement)


@pytest.mark.integration
@pytest.mark.needs_ssh
@pytest.mark.skipif(not all(shutil.which(x) for x in ("ssh-agent", "ssh-add", "ssh-keygen")),
                    reason="ssh tooling unavailable")
def test_real_agent_signal_failure_retains_socket_then_retry(tmp_path, monkeypatch):
    key = tmp_path / "key"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(key)],
                   check=True)
    handle = sd.SshAgentDelivery(sd.SecretValue(key.read_bytes()),
                                 runtime_root=str(tmp_path / "rt"), ttl=60)
    handle.deliver()
    sock, proc = handle.auth_sock, handle._agent_process
    try:
        with monkeypatch.context() as patch:
            def denied(*args):
                raise PermissionError("signal denied")
            patch.setattr(sd.os, "killpg", denied)
            with pytest.raises(PermissionError):
                handle.scrub()
        assert not handle.scrubbed and handle._secret.wiped
        assert handle._agent_process is proc and handle.auth_sock == sock
        assert os.path.exists(sock)
        listing = subprocess.run(["ssh-add", "-l"], capture_output=True,
                                 env=dict(os.environ, SSH_AUTH_SOCK=sock))
        assert listing.returncode == 0
        handle.scrub()
        assert handle.scrubbed and not os.path.exists(sock)
        assert proc.returncode is not None
    finally:
        if not handle.scrubbed:
            handle.scrub()


def test_tmpfs_unmount_denied_keeps_mount_identity_then_retry(tmp_path):
    attempts = []
    def unmount(target):
        attempts.append(target)
        if len(attempts) == 1:
            raise sd.DeliveryError("busy mount")
    handle = sd.TmpfsMountDelivery(sd.SecretValue(b"dummy"), runtime_root=str(tmp_path),
                                   mounter=(lambda *args: None, unmount))
    handle.deliver()
    directory = handle._dir
    with pytest.raises(sd.DeliveryError):
        handle.scrub()
    assert handle._mounted and handle._dir == directory
    assert not handle.scrubbed and handle._secret.wiped
    handle.scrub()
    assert attempts == [directory, directory]
    assert not os.path.exists(directory) and handle.scrubbed


@pytest.mark.parametrize("returncode,still_mounted", [(1, True), (0, True)])
def test_actual_unmount_boundary_requires_disappearance(monkeypatch, returncode, still_mounted):
    calls = []
    monkeypatch.setattr(sd.os.path, "ismount", lambda target: still_mounted)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode)
    monkeypatch.setattr(sd.subprocess, "run", run)
    with pytest.raises(sd.DeliveryError, match="not confirmed"):
        sd._unmount_confirmed("/run/fake-tmpfs")
    assert len(calls) == 1 and calls[0][0] == ["umount", "/run/fake-tmpfs"]
    assert calls[0][1]["timeout"] == 5.0


def test_reaper_does_not_count_failed_mount_or_unknown_agent(tmp_path, monkeypatch, caplog):
    mount = tmp_path / "tmpfs-stale"
    agent = tmp_path / "ssh-stale"
    mount.mkdir(); agent.mkdir()
    monkeypatch.setattr(sd.os.path, "ismount", lambda path: path == str(mount))
    monkeypatch.setattr(sd.subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, 1))
    assert sd.reap_runtime_root(str(tmp_path)) == 0
    assert mount.exists() and agent.exists()
    assert "unresolved workflow secret residue" in caplog.text
    assert "failed" in caplog.text


def test_reaper_counts_only_removed_directory_and_does_not_follow_symlink(tmp_path):
    root = tmp_path / "rt"; root.mkdir()
    stale = root / "tmpfs-stale"; stale.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    (outside / "keep").write_text("dummy")
    (root / "tmpfs-link").symlink_to(outside, target_is_directory=True)
    assert sd.reap_runtime_root(str(root)) == 1
    assert not stale.exists() and (outside / "keep").exists()


@pytest.mark.integration
def test_external_os_reaper_invalidates_group_identity(monkeypatch):
    handle = sd.EnvDelivery(sd.SecretValue(b"dummy"), var="SECRET",
                            command=[sys.executable, "-c", "pass"])
    handle.deliver()
    proc = handle._process
    os.waitpid(proc.pid, 0)  # Popen.returncode remains unset after external reaping
    signals = []
    monkeypatch.setattr(sd.os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(sd.DeliveryError, match="identity cannot be verified"):
        handle.scrub()
    assert signals == [] and not handle.scrubbed
    assert handle._process is proc and handle._secret.wiped
    proc.returncode = 0  # no child left for Popen's destructor to reap


@pytest.mark.integration
def test_group_disappearance_timeout_keeps_anchor_for_retry(monkeypatch):
    handle = sd.EnvDelivery(sd.SecretValue(b"dummy"), var="SECRET",
                            command=[sys.executable, "-c", "pass"])
    handle.deliver()
    proc = handle._process
    with monkeypatch.context() as patch:
        ticks = iter([0.0, 3.0])
        patch.setattr(sd.time, "monotonic", lambda: next(ticks))
        patch.setattr(sd, "_group_alive", lambda pgid: True)
        with pytest.raises(sd.DeliveryError, match="still live"):
            handle.scrub()
    assert proc.returncode is None and handle._process is proc
    assert not handle.scrubbed and handle._secret.wiped
    handle.scrub()
    assert handle.scrubbed and proc.returncode is not None


def test_directory_removal_failure_keeps_retry_identity(tmp_path, monkeypatch):
    handle = sd.TmpfsMountDelivery(sd.SecretValue(b"dummy"), runtime_root=str(tmp_path),
                                   mounter=(lambda *args: None, lambda *args: None))
    handle.deliver()
    directory = handle._dir
    with monkeypatch.context() as patch:
        def denied(path):
            raise PermissionError("removal denied")
        patch.setattr(sd.shutil, "rmtree", denied)
        with pytest.raises(PermissionError):
            handle.scrub()
    assert handle._dir == directory and not handle.scrubbed
    assert not handle._mounted and handle._secret.wiped
    handle.scrub()
    assert handle.scrubbed and not os.path.exists(directory)


def test_reaper_failed_removal_is_not_counted(tmp_path, monkeypatch):
    stale = tmp_path / "tmpfs-stale"; stale.mkdir()
    def denied(path):
        raise PermissionError("removal denied")
    monkeypatch.setattr(sd.shutil, "rmtree", denied)
    assert sd.reap_runtime_root(str(tmp_path)) == 0
    assert stale.exists()


def test_mount_timeout_retains_attempted_mount_until_verified_retry(tmp_path, monkeypatch):
    mounted = set()
    commands = []
    unmount_attempts = []
    def run(command, **kwargs):
        commands.append(command)
        target = command[-1]
        if command[0] == "mount":
            mounted.add(target)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        unmount_attempts.append(target)
        if len(unmount_attempts) == 1:
            return subprocess.CompletedProcess(command, 1)
        mounted.remove(target)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(sd.subprocess, "run", run)
    monkeypatch.setattr(sd.os.path, "ismount", lambda target: target in mounted)
    handle = sd.TmpfsMountDelivery(sd.SecretValue(b"dummy"), runtime_root=str(tmp_path))
    with pytest.raises(sd.DeliveryError, match="not confirmed"):
        handle.deliver()
    assert handle._mounted and handle._dir in mounted
    assert not handle.scrubbed and handle.path is None
    directory = handle._dir
    handle.scrub()
    assert unmount_attempts == [directory, directory]
    assert not mounted and not os.path.exists(directory)
    assert handle.scrubbed and handle._secret.wiped
