"""Secret delivery mechanisms for the workflow engine.

A workflow's ``deliver_secret`` step hands a vault item to a privileged
task through one of four narrow channels, each with a matching scrub
that *actually revokes* the secret rather than just forgetting it:

  - ``env``        — environment variable on a spawned child command.
  - ``ssh-agent``  — ephemeral ssh-agent socket holding the key.
  - ``fd-pass``    — secret written to a pipe whose read end is inherited
                     by a spawned child.
  - ``tmpfs-mount``— secret file on a per-run tmpfs, mode 0600, owned by
                     the consuming role; unmounted + wiped on scrub.

Security invariants (the crown jewel — see permissions.md §"Secret
delivery to privileged tasks"):

  - The plaintext secret is held only in a wipeable ``SecretValue``
    buffer and, transiently, in whatever channel the method requires
    (a child's env, a pipe, an agent, a tmpfs file). It is never written
    to a world-readable path and never returned in metadata.
  - ``scrub()`` is idempotent and best-effort-total: it revokes the
    channel (kill agent, close fds, umount+unlink) and wipes the buffer.
  - Mechanisms that cannot guarantee RAM-only storage fail closed: the
    tmpfs method raises rather than fall back to an on-disk file.
  - Defense-in-depth against engine crash: the ssh-agent key is added
    with a TTL so it self-expires even if ``scrub()`` never runs.

Callers must treat ``metadata()`` as the only loggable surface.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("qdistro.workflow.secret")

# Per-run RAM root. Subdirs are created 0700; tmpfs is mounted on top.
_DEFAULT_RUNTIME_ROOT = "/run/qdistro/workflow-secrets"

# ssh-agent key TTL (seconds): even if scrub() is skipped on a crash the
# loaded key self-expires.
_SSH_KEY_TTL_DEFAULT = 900

# Method name aliases accepted in YAML.
_METHOD_ALIASES = {
    "env": "env",
    "env_var": "env",
    "ssh-agent": "ssh-agent",
    "ssh_agent": "ssh-agent",
    "ssh_agent_socket": "ssh-agent",
    "fd-pass": "fd-pass",
    "fd_pass": "fd-pass",
    "fd": "fd-pass",
    "tmpfs-mount": "tmpfs-mount",
    "tmpfs_mount": "tmpfs-mount",
    "tmpfs": "tmpfs-mount",
}


class DeliveryError(Exception):
    """Raised when a secret cannot be delivered (fail-closed)."""


def _run_in_new_session(command: list[str], env: dict[str, str],
                        pass_fds: tuple[int, ...] = ()) -> tuple[int, int]:
    """Spawn ``command`` as a new session leader and wait for it.

    Returning the leader pid lets the caller ``killpg`` the whole group on
    scrub so a backgrounded descendant cannot outlive the secret. Returns
    ``(returncode, pgid)``.
    """
    proc = subprocess.Popen(  # noqa: S603
        command, env=env, pass_fds=pass_fds, start_new_session=True)
    pgid = proc.pid  # session leader: pgid == pid
    proc.wait()
    return proc.returncode, pgid


def _killpg(pgid: int | None) -> None:
    if pgid is None:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


class SecretValue:
    """A secret held in a wipeable buffer.

    Keep the lifetime minimal: read ``.as_bytes()``/``.as_str()`` at the
    moment of use, then ``wipe()``. The buffer is a ``bytearray`` so the
    bytes can be zeroed in place; Python ``str``/``bytes`` copies cannot,
    so we avoid making them except transiently at the channel boundary.
    """

    __slots__ = ("_buf", "_wiped")

    def __init__(self, data: bytes):
        self._buf = bytearray(data)
        self._wiped = False

    def as_bytes(self) -> bytes:
        if self._wiped:
            raise DeliveryError("secret already wiped")
        return bytes(self._buf)

    def as_str(self) -> str:
        return self.as_bytes().decode("utf-8")

    def __len__(self) -> int:
        return 0 if self._wiped else len(self._buf)

    def wipe(self) -> None:
        for i in range(len(self._buf)):
            self._buf[i] = 0
        del self._buf[:]
        self._wiped = True

    @property
    def wiped(self) -> bool:
        return self._wiped


class DeliveryHandle(ABC):
    """Live delivery whose ``scrub()`` revokes the secret channel."""

    method = "base"

    def __init__(self, secret: SecretValue):
        self._secret = secret
        self._scrubbed = False
        self._lock = threading.Lock()

    @abstractmethod
    def _revoke(self) -> None:
        """Method-specific teardown (kill agent, close fds, umount...)."""

    def scrub(self) -> None:
        """Revoke the channel and wipe the buffer. Idempotent."""
        with self._lock:
            if self._scrubbed:
                return
            self._scrubbed = True
            try:
                self._revoke()
            except Exception as e:  # noqa: BLE001
                logger.warning("scrub revoke for %s failed: %r",
                               self.method, e)
            finally:
                self._secret.wipe()

    @property
    def scrubbed(self) -> bool:
        return self._scrubbed

    def metadata(self) -> dict[str, Any]:
        """Loggable, secret-free description of this delivery."""
        return {"method": self.method, "scrubbed": self._scrubbed}


# ----------------------------------------------------------------------
# env
# ----------------------------------------------------------------------


class EnvDelivery(DeliveryHandle):
    """Deliver the secret as an env var to a spawned child command.

    The plaintext exists only in the child's environment and the
    wipeable buffer; nothing is written to disk. If no ``command`` is
    given the overlay is prepared but no process is spawned (a later
    exec step would consume it).

    Known tradeoff: while the child lives, its environment is readable
    via ``/proc/<pid>/environ`` by the same uid and root, and scrub()
    cannot retract it from an already-spawned process. This is inherent
    to env-on-exec; callers that need a tighter blast radius should
    prefer ``fd-pass`` or ``ssh-agent``. ``env`` is offered because some
    tools only accept secrets via the environment.
    """

    method = "env"

    def __init__(self, secret: SecretValue, *, var: str,
                 command: list[str] | None = None,
                 base_env: dict[str, str] | None = None):
        super().__init__(secret)
        if not var:
            raise DeliveryError("env delivery requires a 'var' name")
        self._var = var
        self._command = command
        self._base_env = base_env
        self._pgid: int | None = None
        self.returncode: int | None = None

    def deliver(self) -> None:
        if not self._command:
            return
        env = dict(self._base_env if self._base_env is not None else os.environ)
        env[self._var] = self._secret.as_str()
        try:
            self.returncode, self._pgid = _run_in_new_session(
                self._command, env)
        finally:
            # Drop our reference to the plaintext-bearing env dict.
            env[self._var] = ""
            del env
        if self.returncode != 0:
            raise DeliveryError(
                f"env command exited {self.returncode}")

    def environ_overlay(self) -> dict[str, str]:
        """Overlay for a caller-managed exec. Exposes the secret — use
        only at the moment of spawning, never log the result."""
        return {self._var: self._secret.as_str()}

    def _revoke(self) -> None:
        # Kill any backgrounded descendant still holding the secret in its
        # environment; the buffer wipe in scrub() handles our own copy.
        _killpg(self._pgid)
        self._pgid = None

    def metadata(self) -> dict[str, Any]:
        md = super().metadata()
        md["var"] = self._var
        md["spawned"] = bool(self._command)
        return md


# ----------------------------------------------------------------------
# fd-pass
# ----------------------------------------------------------------------


class FdPassDelivery(DeliveryHandle):
    """Pass the secret to a child via an inherited read-only pipe fd.

    The secret bytes are written into a pipe; the read end is inherited
    by the spawned child (``pass_fds``), with the fd number provided in
    ``SECRET_FD`` (overridable). The write end is closed after writing so
    the child sees EOF after the secret. scrub() closes the read end.
    """

    method = "fd-pass"

    def __init__(self, secret: SecretValue, *, command: list[str] | None = None,
                 fd_env: str = "SECRET_FD",
                 base_env: dict[str, str] | None = None):
        super().__init__(secret)
        self._command = command
        self._fd_env = fd_env
        self._base_env = base_env
        self._read_fd: int | None = None
        self._pgid: int | None = None
        self.returncode: int | None = None

    # Bound well under a pipe's typical 64 KiB capacity: we write the
    # whole secret before any reader exists, so a secret larger than the
    # buffer would block os.write indefinitely. 32 KiB covers every key
    # type with margin.
    _MAX_SECRET_BYTES = 32 * 1024

    def deliver(self) -> None:
        secret = self._secret.as_bytes()
        if len(secret) > self._MAX_SECRET_BYTES:
            raise DeliveryError(
                f"fd-pass secret exceeds {self._MAX_SECRET_BYTES}-byte budget")
        read_fd, write_fd = os.pipe()
        self._read_fd = read_fd
        try:
            os.write(write_fd, secret)
        finally:
            os.close(write_fd)
        if not self._command:
            return
        os.set_inheritable(read_fd, True)
        env = dict(self._base_env if self._base_env is not None else os.environ)
        env[self._fd_env] = str(read_fd)
        try:
            self.returncode, self._pgid = _run_in_new_session(
                self._command, env, pass_fds=(read_fd,))
        finally:
            del env
        if self.returncode != 0:
            raise DeliveryError(f"fd-pass command exited {self.returncode}")

    @property
    def read_fd(self) -> int | None:
        return self._read_fd

    def _revoke(self) -> None:
        # Kill any backgrounded descendant that may still hold the
        # inherited read fd, then close our own end.
        _killpg(self._pgid)
        self._pgid = None
        if self._read_fd is not None:
            try:
                os.close(self._read_fd)
            except OSError:
                pass
            self._read_fd = None


# ----------------------------------------------------------------------
# ssh-agent
# ----------------------------------------------------------------------


class SshAgentDelivery(DeliveryHandle):
    """Hold the key in a per-run ssh-agent; SSH_AUTH_SOCK exposed.

    The key is piped to ``ssh-add -`` over stdin (no temp file) with a
    TTL so it self-expires on crash. scrub() kills the agent and removes
    its socket directory.
    """

    method = "ssh-agent"

    def __init__(self, secret: SecretValue, *, runtime_root: str | None = None,
                 ttl: int = _SSH_KEY_TTL_DEFAULT,
                 ssh_agent_bin: str = "ssh-agent",
                 ssh_add_bin: str = "ssh-add"):
        super().__init__(secret)
        self._runtime_root = runtime_root or _DEFAULT_RUNTIME_ROOT
        self._ttl = max(int(ttl), 1)
        self._agent_bin = ssh_agent_bin
        self._add_bin = ssh_add_bin
        self._dir: str | None = None
        self._sock: str | None = None
        self._agent_pid: int | None = None

    def deliver(self) -> None:
        if shutil.which(self._agent_bin) is None:
            raise DeliveryError(f"{self._agent_bin} not found")
        os.makedirs(self._runtime_root, mode=0o700, exist_ok=True)
        self._dir = tempfile.mkdtemp(prefix="ssh-", dir=self._runtime_root)
        os.chmod(self._dir, 0o700)
        self._sock = os.path.join(self._dir, "agent.sock")
        # Start the agent bound to our socket.
        proc = subprocess.run(  # noqa: S603
            [self._agent_bin, "-a", self._sock],
            capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise DeliveryError(f"ssh-agent failed: {proc.returncode}")
        for line in proc.stdout.splitlines():
            if line.startswith("SSH_AGENT_PID="):
                pid_s = line.split("=", 1)[1].split(";", 1)[0]
                try:
                    self._agent_pid = int(pid_s)
                except ValueError:
                    pass
        # Load the key from stdin with a TTL.
        env = dict(os.environ, SSH_AUTH_SOCK=self._sock)
        add = subprocess.run(  # noqa: S603
            [self._add_bin, "-t", str(self._ttl), "-"],
            input=self._secret.as_bytes(), env=env,
            capture_output=True, check=False)
        if add.returncode != 0:
            # Fail closed: tear the agent down, surface no key material.
            self._revoke()
            raise DeliveryError("ssh-add rejected the key")

    @property
    def auth_sock(self) -> str | None:
        return self._sock

    def _revoke(self) -> None:
        if self._agent_pid is not None:
            try:
                os.kill(self._agent_pid, 15)
            except OSError:
                pass
            self._agent_pid = None
        if self._dir is not None and os.path.isdir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self._sock = None


# ----------------------------------------------------------------------
# tmpfs-mount
# ----------------------------------------------------------------------


class TmpfsMountDelivery(DeliveryHandle):
    """Expose the secret as a 0600 file on a per-run tmpfs.

    Fails closed: if a tmpfs cannot be mounted (no privilege) it raises
    rather than writing the secret to a persistent on-disk path. scrub()
    overwrites the file, unmounts the tmpfs, and removes the directory.
    """

    method = "tmpfs-mount"

    def __init__(self, secret: SecretValue, *, runtime_root: str | None = None,
                 owner_uid: int | None = None, filename: str = "secret",
                 size: str = "1m", mounter: Any | None = None):
        super().__init__(secret)
        self._runtime_root = runtime_root or _DEFAULT_RUNTIME_ROOT
        self._owner_uid = owner_uid
        # Reject anything that isn't a bare filename: an absolute path or
        # a "../" would let the plaintext escape the tmpfs onto persistent
        # storage, defeating fail-closed.
        if (not filename or os.path.basename(filename) != filename
                or filename in (".", "..")):
            raise DeliveryError(f"unsafe tmpfs filename {filename!r}")
        self._filename = filename
        self._size = size
        # Injectable mount/umount for tests: (mount_fn, umount_fn).
        self._mounter = mounter
        self._dir: str | None = None
        self._path: str | None = None
        self._mounted = False

    def _do_mount(self, target: str) -> None:
        if self._mounter is not None:
            self._mounter[0](target, self._size)
            return
        proc = subprocess.run(  # noqa: S603
            ["mount", "-t", "tmpfs", "-o",
             f"size={self._size},mode=0700", "tmpfs", target],
            capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise DeliveryError(
                f"tmpfs mount failed (need privilege): "
                f"{proc.stderr.strip() or proc.returncode}")

    def _do_umount(self, target: str) -> None:
        if self._mounter is not None:
            self._mounter[1](target)
            return
        r = subprocess.run(["umount", target],  # noqa: S603
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            # A busy mount must not pin the (already-zeroed) secret dir.
            # Lazy-detach so it is released as soon as the last user exits.
            lazy = subprocess.run(["umount", "-l", target],  # noqa: S603
                                  capture_output=True, text=True, check=False)
            if lazy.returncode != 0:
                logger.error(
                    "tmpfs umount of %s failed (%s) and lazy detach failed "
                    "(%s); secret file content was zeroed before umount",
                    target, r.stderr.strip(), lazy.stderr.strip())

    def deliver(self) -> None:
        os.makedirs(self._runtime_root, mode=0o700, exist_ok=True)
        self._dir = tempfile.mkdtemp(prefix="tmpfs-", dir=self._runtime_root)
        os.chmod(self._dir, 0o700)
        try:
            self._do_mount(self._dir)
            self._mounted = True
        except DeliveryError:
            # No tmpfs -> do NOT write plaintext to a persistent path.
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
            raise
        self._path = os.path.join(self._dir, self._filename)
        try:
            # Write 0600 before anyone can open it.
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
            try:
                os.write(fd, self._secret.as_bytes())
            finally:
                os.close(fd)
            if self._owner_uid is not None:
                try:
                    os.chown(self._path, self._owner_uid, -1)
                    os.chown(self._dir, self._owner_uid, -1)
                except OSError as e:
                    logger.warning("tmpfs chown to uid %s failed: %r",
                                   self._owner_uid, e)
        except Exception:
            # A write/chown failure after mount must not leave a mounted
            # tmpfs or a partial plaintext file behind.
            self._revoke()
            raise

    @property
    def path(self) -> str | None:
        return self._path

    def _revoke(self) -> None:
        # Overwrite the file content before unmounting (cheap, RAM-backed).
        if self._path is not None and os.path.isfile(self._path):
            try:
                size = os.path.getsize(self._path)
                with open(self._path, "wb") as f:
                    f.write(b"\x00" * size)
                    f.flush()
                    os.fsync(f.fileno())
                os.unlink(self._path)
            except OSError:
                pass
        if self._mounted and self._dir is not None:
            self._do_umount(self._dir)
            self._mounted = False
        if self._dir is not None and os.path.isdir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self._path = None


# ----------------------------------------------------------------------
# factory
# ----------------------------------------------------------------------

_HANDLE_CLASSES = {
    "env": EnvDelivery,
    "fd-pass": FdPassDelivery,
    "ssh-agent": SshAgentDelivery,
    "tmpfs-mount": TmpfsMountDelivery,
}


def reap_runtime_root(root: str = _DEFAULT_RUNTIME_ROOT) -> int:
    """Tear down stale per-run secret dirs left by a crashed engine.

    Best-effort: unmounts any tmpfs still mounted under each leftover
    directory and removes it. Called at engine startup so a hard crash
    can't leave a tmpfs/ssh-agent secret readable until reboot. The root
    is a fixed, root-owned path so this never touches arbitrary mounts.
    Returns the number of entries reaped.
    """
    if not os.path.isdir(root):
        return 0
    reaped = 0
    for entry in os.listdir(root):
        path = os.path.join(root, entry)
        try:
            r = subprocess.run(["umount", path],  # noqa: S603
                               capture_output=True, check=False)
            if r.returncode != 0:
                # Busy mount: lazy-detach so it can't pin the dir.
                subprocess.run(["umount", "-l", path],  # noqa: S603
                               capture_output=True, check=False)
            shutil.rmtree(path, ignore_errors=True)
            reaped += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("reap of stale secret dir %s failed: %r",
                           path, e)
    return reaped


def normalize_method(name: str) -> str:
    method = _METHOD_ALIASES.get(str(name))
    if method is None:
        raise DeliveryError(
            f"unknown delivery method {name!r}; valid: "
            f"{sorted(set(_METHOD_ALIASES.values()))}")
    return method


def make_delivery(method: str, secret: SecretValue,
                  config: dict[str, Any]) -> DeliveryHandle:
    """Build (but do not yet deliver) a handle for the given method.

    ``config`` carries method-specific knobs (var, command, owner_uid,
    runtime_root, ...). Unknown keys are ignored by each handle.
    """
    method = normalize_method(method)
    if method == "env":
        return EnvDelivery(
            secret, var=config.get("var", ""),
            command=config.get("command"),
            base_env=config.get("base_env"))
    if method == "fd-pass":
        return FdPassDelivery(
            secret, command=config.get("command"),
            fd_env=config.get("fd_env", "SECRET_FD"),
            base_env=config.get("base_env"))
    if method == "ssh-agent":
        return SshAgentDelivery(
            secret, runtime_root=config.get("runtime_root"),
            ttl=int(config.get("ttl", _SSH_KEY_TTL_DEFAULT)))
    if method == "tmpfs-mount":
        return TmpfsMountDelivery(
            secret, runtime_root=config.get("runtime_root"),
            owner_uid=config.get("owner_uid"),
            filename=config.get("filename", "secret"),
            size=config.get("size", "1m"),
            mounter=config.get("mounter"))
    raise DeliveryError(f"no handler for method {method!r}")  # unreachable
