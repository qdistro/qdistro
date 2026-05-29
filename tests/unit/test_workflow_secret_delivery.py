"""Unit tests for workflow secret delivery (Phase 2, security-critical).

Each mechanism is exercised for real where headless-feasible:
  - env / fd-pass spawn a child and prove the secret arrived only via
    the intended channel.
  - ssh-agent runs a real per-run agent (skipped if ssh tooling absent).
  - tmpfs-mount is tested fail-closed (no privilege -> raises, nothing
    written) and via an injected mounter for the write/scrub path.

The engine-integration tests prove the secret never lands in the audit
DB, the step details, or the logs in clear, and that scrub revokes on
both success and mid-step failure.
"""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "workflow"))

from secret_delivery import (  # noqa: E402
    DeliveryError,
    EnvDelivery,
    FdPassDelivery,
    SecretValue,
    SshAgentDelivery,
    TmpfsMountDelivery,
    make_delivery,
    normalize_method,
)
from pwd_secret_source import parse_item  # noqa: E402
from workflow_schema import (  # noqa: E402
    StepDef, StepType, TriggerDef, TriggerType, WorkflowDef, RunState,
)
from workflow_engine import WorkflowEngine  # noqa: E402
from audit_logger import WorkflowAuditLogger  # noqa: E402

SECRET = b"super-secret-key-material-ABC123"


# ======================================================================
# SecretValue
# ======================================================================


class TestSecretValue:
    def test_roundtrip_and_wipe(self):
        s = SecretValue(SECRET)
        assert s.as_bytes() == SECRET
        assert s.as_str() == SECRET.decode()
        assert len(s) == len(SECRET)
        s.wipe()
        assert s.wiped
        assert len(s) == 0
        with pytest.raises(DeliveryError):
            s.as_bytes()

    def test_wipe_is_idempotent(self):
        s = SecretValue(SECRET)
        s.wipe()
        s.wipe()  # must not raise


# ======================================================================
# method normalization
# ======================================================================


class TestNormalize:
    def test_aliases(self):
        assert normalize_method("ssh_agent_socket") == "ssh-agent"
        assert normalize_method("env") == "env"
        assert normalize_method("fd") == "fd-pass"
        assert normalize_method("tmpfs") == "tmpfs-mount"

    def test_unknown(self):
        with pytest.raises(DeliveryError):
            normalize_method("carrier-pigeon")


# ======================================================================
# env
# ======================================================================


class TestEnvDelivery:
    def test_spawn_receives_secret(self, tmp_path):
        out = tmp_path / "captured"
        cmd = [sys.executable, "-c",
               f"import os;open({str(out)!r},'w').write(os.environ['THE_VAR'])"]
        s = SecretValue(SECRET)
        d = EnvDelivery(s, var="THE_VAR", command=cmd, base_env={})
        d.deliver()
        assert out.read_bytes() == SECRET
        d.scrub()
        assert s.wiped

    def test_overlay_without_command(self):
        s = SecretValue(SECRET)
        d = EnvDelivery(s, var="THE_VAR")
        d.deliver()
        assert d.environ_overlay() == {"THE_VAR": SECRET.decode()}
        d.scrub()

    def test_nonzero_exit_raises(self):
        s = SecretValue(SECRET)
        d = EnvDelivery(s, var="X", command=[sys.executable, "-c", "import sys;sys.exit(3)"],
                        base_env={})
        with pytest.raises(DeliveryError):
            d.deliver()

    def test_missing_var(self):
        with pytest.raises(DeliveryError):
            EnvDelivery(SecretValue(SECRET), var="")

    def test_scrub_kills_backgrounded_descendant(self, tmp_path):
        import time
        marker = tmp_path / "childpid"
        # The command forks a long-lived child (still in the session's
        # process group) and the leader exits immediately.
        script = (
            "import os,sys,time\n"
            "pid=os.fork()\n"
            "if pid==0:\n"
            f"    open({str(marker)!r},'w').write(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "sys.exit(0)\n"
        )
        s = SecretValue(SECRET)
        d = EnvDelivery(s, var="THE_VAR",
                        command=[sys.executable, "-c", script], base_env={})
        d.deliver()
        # Wait for the descendant to register itself.
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        child_pid = int(marker.read_text())
        os.kill(child_pid, 0)  # alive (raises if not)
        d.scrub()
        # killpg should have reaped the descendant.
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except OSError:
                break
            time.sleep(0.02)
        with pytest.raises(OSError):
            os.kill(child_pid, 0)

    def test_metadata_has_no_secret(self):
        s = SecretValue(SECRET)
        d = EnvDelivery(s, var="THE_VAR")
        md = d.metadata()
        assert SECRET.decode() not in str(md)
        assert md["method"] == "env"


# ======================================================================
# fd-pass
# ======================================================================


class TestFdPassDelivery:
    def test_child_reads_fd(self, tmp_path):
        out = tmp_path / "captured"
        script = (
            "import os;fd=int(os.environ['SECRET_FD']);"
            f"open({str(out)!r},'wb').write(os.read(fd, 4096))"
        )
        s = SecretValue(SECRET)
        d = FdPassDelivery(s, command=[sys.executable, "-c", script],
                           base_env={})
        d.deliver()
        assert out.read_bytes() == SECRET
        d.scrub()
        assert s.wiped

    def test_scrub_closes_fd(self):
        s = SecretValue(SECRET)
        d = FdPassDelivery(s)
        d.deliver()  # no command: pipe created, write end closed
        fd = d.read_fd
        assert fd is not None
        d.scrub()
        # fd is closed now.
        with pytest.raises(OSError):
            os.fstat(fd)

    def test_oversize_rejected(self):
        big = SecretValue(b"x" * (64 * 1024 + 1))
        d = FdPassDelivery(big)
        with pytest.raises(DeliveryError):
            d.deliver()


# ======================================================================
# ssh-agent (real, skipped without tooling)
# ======================================================================

_HAVE_SSH = all(shutil.which(b) for b in ("ssh-agent", "ssh-add", "ssh-keygen"))


@pytest.mark.skipif(not _HAVE_SSH, reason="ssh tooling not available")
class TestSshAgentDelivery:
    def _gen_key(self, tmp_path) -> bytes:
        kp = tmp_path / "id_ed25519"
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                        "-f", str(kp)], check=True)
        data = kp.read_bytes()
        return data

    def test_agent_holds_key_then_scrub(self, tmp_path):
        key = self._gen_key(tmp_path)
        runtime = tmp_path / "rt"
        s = SecretValue(key)
        d = SshAgentDelivery(s, runtime_root=str(runtime), ttl=60)
        d.deliver()
        sock = d.auth_sock
        assert sock and os.path.exists(sock)
        listing = subprocess.run(
            ["ssh-add", "-l"], env=dict(os.environ, SSH_AUTH_SOCK=sock),
            capture_output=True, text=True)
        assert listing.returncode == 0  # 0 = has identities
        d.scrub()
        assert s.wiped
        assert not os.path.exists(sock)

    def test_key_without_trailing_newline_accepted(self, tmp_path):
        # A vault that stores the PEM value rstrip()'d drops the trailing
        # newline OpenSSH requires; the agent delivery must normalise it
        # rather than fail with "ssh-add rejected the key". (Found during
        # VM validation against a real pwd vault.)
        key = self._gen_key(tmp_path).rstrip(b"\n")
        assert not key.endswith(b"\n")
        runtime = tmp_path / "rt"
        s = SecretValue(key)
        d = SshAgentDelivery(s, runtime_root=str(runtime), ttl=60)
        d.deliver()  # must NOT raise
        sock = d.auth_sock
        listing = subprocess.run(
            ["ssh-add", "-l"], env=dict(os.environ, SSH_AUTH_SOCK=sock),
            capture_output=True, text=True)
        assert listing.returncode == 0  # key really loaded
        d.scrub()
        assert not os.path.exists(sock)


# ======================================================================
# tmpfs-mount
# ======================================================================


class TestTmpfsMountDelivery:
    def test_fail_closed_without_privilege(self, tmp_path):
        # Real mount almost certainly fails for the test uid -> must
        # raise AND leave no plaintext behind.
        if os.geteuid() == 0:
            pytest.skip("running as root; cannot exercise the deny path")
        s = SecretValue(SECRET)
        d = TmpfsMountDelivery(s, runtime_root=str(tmp_path / "rt"))
        with pytest.raises(DeliveryError):
            d.deliver()
        # No file was written anywhere under the runtime root.
        leaked = [p for p in (tmp_path / "rt").rglob("*") if p.is_file()] \
            if (tmp_path / "rt").exists() else []
        assert leaked == []

    def test_rejects_path_traversal_filename(self, tmp_path):
        for bad in ("/var/tmp/secret", "../escape", "a/b", ".", ".."):
            with pytest.raises(DeliveryError):
                TmpfsMountDelivery(SecretValue(SECRET),
                                   runtime_root=str(tmp_path), filename=bad)

    def test_partial_write_failure_unmounts(self, tmp_path):
        # Mount succeeds (injected) but the file write fails because the
        # filename collides with a pre-created directory -> must revoke
        # (umount) and leave nothing behind.
        mounts, umounts = [], []
        mounter = (lambda target, size: mounts.append(target),
                   lambda target: umounts.append(target))
        s = SecretValue(SECRET)
        d = TmpfsMountDelivery(s, runtime_root=str(tmp_path / "rt"),
                               mounter=mounter, filename="secret")

        # Make os.open fail after the (fake) mount by pre-creating a dir
        # at the target path inside deliver via monkeypatching mkdtemp?
        # Simpler: drive deliver, then assert; here we force failure by
        # pointing the write at a path that already exists as a dir.
        import unittest.mock as mock
        with mock.patch("os.open", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                d.deliver()
        assert umounts  # tmpfs was unmounted on the failed write
        assert d.path is None or not os.path.exists(d.path)

    def test_write_and_scrub_with_injected_mounter(self, tmp_path):
        mounts, umounts = [], []
        mounter = (lambda target, size: mounts.append(target),
                   lambda target: umounts.append(target))
        s = SecretValue(SECRET)
        d = TmpfsMountDelivery(s, runtime_root=str(tmp_path / "rt"),
                               mounter=mounter)
        d.deliver()
        path = d.path
        assert path and os.path.isfile(path)
        assert open(path, "rb").read() == SECRET
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600
        assert mounts  # mount was attempted
        d.scrub()
        assert s.wiped
        assert not os.path.exists(path)
        assert umounts  # umount was called


# ======================================================================
# make_delivery factory
# ======================================================================


def test_make_delivery_builds_handles():
    s = SecretValue(SECRET)
    h = make_delivery("ssh_agent_socket", s, {"runtime_root": "/tmp/x"})
    assert isinstance(h, SshAgentDelivery)


# ======================================================================
# item path parsing
# ======================================================================


class TestParseItem:
    def test_basic(self):
        assert parse_item("vault/dev/github-ssh-key") == ("dev", "github-ssh-key")

    def test_no_prefix(self):
        assert parse_item("dev/key") == ("dev", "key")

    def test_tag_with_slash(self):
        assert parse_item("vault/dev/portal/app") == ("dev", "portal/app")

    @pytest.mark.parametrize("bad", ["", "vault/", "dev", "vault/dev"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            parse_item(bad)


# ======================================================================
# Engine integration — delivery + scrub + no-leak
# ======================================================================


class _FakeSource:
    def __init__(self, secret: bytes):
        self.secret = secret
        self.calls = []

    def fetch(self, item, run_id=""):
        self.calls.append((item, run_id))
        return self.secret


class _AllowHooks:
    def query(self, action, event):
        return {"verdict": "allow"}


class _AllowBroker:
    """Broker proxy whose hooks always allow (so run_hook steps succeed)."""
    hooks = _AllowHooks()


def _engine_with_secret(tmp_path, command, *, fail_after=False):
    audit = WorkflowAuditLogger(db_path=str(tmp_path / "audit.sqlite"))
    engine = WorkflowEngine(audit_logger=audit,
                            secret_source=_FakeSource(SECRET))
    steps = [StepDef(type=StepType.DELIVER_SECRET, name="deliver", config={
        "item": "vault/dev/key", "as": "env", "var": "THE_VAR",
        "command": command,
    })]
    if fail_after:
        steps.append(StepDef(type=StepType.RUN_HOOK, name="boom",
                             config={"hook": "explode"}))
    engine._workflows["wf"] = WorkflowDef(
        name="wf", trigger=TriggerDef(type=TriggerType.CRON), steps=steps,
        needs=["vault/dev/key"])
    return engine, audit


def _audit_text(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    chunks = []
    for tbl in ("workflow_runs", "workflow_steps", "workflow_audit"):
        for row in conn.execute(f"SELECT * FROM {tbl}"):
            chunks.append("|".join(str(c) for c in row))
    conn.close()
    return "\n".join(chunks)


class TestEngineDelivery:
    def test_deliver_and_scrub_on_success(self, tmp_path, caplog):
        out = tmp_path / "captured"
        cmd = [sys.executable, "-c",
               f"import os;open({str(out)!r},'w').write(os.environ['THE_VAR'])"]
        engine, audit = _engine_with_secret(tmp_path, cmd)
        with caplog.at_level("INFO"):
            run = engine.start_run("wf")
        assert run.state == RunState.COMPLETED
        # The child actually received the secret via env.
        assert out.read_bytes() == SECRET
        # Handles cleaned up (scrubbed) after success.
        assert run.run_id not in engine._delivery_handles
        # The secret never appears in the audit DB...
        assert SECRET.decode() not in _audit_text(audit.db_path)
        # ...nor in the step details recorded on the run...
        details = run.steps_completed[0].details
        assert SECRET.decode() not in str(details)
        assert details["delivered"] is True
        # ...nor in the logs.
        assert SECRET.decode() not in caplog.text

    def test_scrub_on_mid_step_failure(self, tmp_path):
        out = tmp_path / "captured"
        cmd = [sys.executable, "-c",
               f"import os;open({str(out)!r},'w').write(os.environ['THE_VAR'])"]
        engine, audit = _engine_with_secret(tmp_path, cmd, fail_after=True)

        # Force the second step to fail.
        orig = engine._handle_run_hook

        def boom(step, run, result, wf=None):
            result.success = False
            result.error = "kaboom"
        engine._handle_run_hook = boom  # type: ignore[assignment]

        run = engine.start_run("wf")
        assert run.state == RunState.FAILED
        # Delivery handle was scrubbed despite the later failure.
        assert run.run_id not in engine._delivery_handles
        assert SECRET.decode() not in _audit_text(audit.db_path)

    def test_scrub_all_runs_on_shutdown(self, tmp_path):
        # A delivery with no command leaves a live handle we can inspect.
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        engine = WorkflowEngine(audit_logger=audit,
                                secret_source=_FakeSource(SECRET))
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["vault/dev/key"],
            steps=[StepDef(type=StepType.DELIVER_SECRET, config={
                "item": "vault/dev/key", "as": "fd-pass"})])
        run = engine.start_run("wf")
        # fd-pass with no command: handle scrubbed at workflow exit already.
        assert run.run_id not in engine._delivery_handles
        # shutdown is safe to call with nothing outstanding.
        engine.shutdown()

    def test_scrub_on_step_exit_scrubs_immediately(self, tmp_path):
        out = tmp_path / "captured"
        cmd = [sys.executable, "-c",
               f"import os;open({str(out)!r},'w').write(os.environ['THE_VAR'])"]
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "audit.sqlite"))
        engine = WorkflowEngine(audit_logger=audit, broker_proxy=_AllowBroker(),
                                secret_source=_FakeSource(SECRET))
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["vault/dev/key"],
            steps=[
                StepDef(type=StepType.DELIVER_SECRET, config={
                    "item": "vault/dev/key", "as": "env", "var": "THE_VAR",
                    "command": cmd, "scrub_on": "step_exit"}),
                StepDef(type=StepType.RUN_HOOK, config={"hook": "noop"}),
            ])
        run = engine.start_run("wf")
        assert run.state == RunState.COMPLETED
        assert out.read_bytes() == SECRET
        # Step-level scrub: nothing left tracked for workflow exit, and
        # the metadata reflects the immediate scrub.
        assert run.run_id not in engine._delivery_handles
        assert run.steps_completed[0].details["scrubbed"] is True

    def test_delivery_failure_scrubs_and_fails_run(self, tmp_path):
        # Child exits nonzero -> delivery fails; the run fails and no
        # handle is left tracked.
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "audit.sqlite"))
        engine = WorkflowEngine(audit_logger=audit,
                                secret_source=_FakeSource(SECRET))
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["vault/dev/key"],
            steps=[StepDef(type=StepType.DELIVER_SECRET, config={
                "item": "vault/dev/key", "as": "env", "var": "X",
                "command": [sys.executable, "-c", "import sys;sys.exit(2)"]})])
        run = engine.start_run("wf")
        assert run.state == RunState.FAILED
        assert run.run_id not in engine._delivery_handles

    def test_delivery_during_shutdown_scrubs_immediately(self, tmp_path):
        # When the engine is stopping, a delivery must scrub at once
        # rather than be tracked into a dict scrub_all_runs already drained.
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        engine = WorkflowEngine(audit_logger=audit,
                                secret_source=_FakeSource(SECRET))
        engine._stopping = True
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["vault/dev/key"],
            steps=[StepDef(type=StepType.DELIVER_SECRET, config={
                "item": "vault/dev/key", "as": "fd-pass"})])
        run = engine.start_run("wf")
        assert run.state == RunState.COMPLETED
        # Not tracked for later — scrubbed in-line.
        assert run.run_id not in engine._delivery_handles
        assert run.steps_completed[0].details["scrubbed"] is True

    def test_tracking_only_without_source(self, tmp_path):
        # No secret_source -> deliver_secret stays in tracking-only mode
        # and never fetches/delivers.
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        engine = WorkflowEngine(audit_logger=audit)
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["vault/dev/key"],
            steps=[StepDef(type=StepType.DELIVER_SECRET, config={
                "item": "vault/dev/key", "as": "env"})])
        run = engine.start_run("wf")
        assert run.state == RunState.COMPLETED
        assert run.steps_completed[0].details["delivered"] is False


# ======================================================================
# Phase 2 — channel publication + consumption loop
# ======================================================================

_HAVE_SSH = all(shutil.which(b) for b in ("ssh-agent", "ssh-add", "ssh-keygen"))


def _gen_ssh_key(tmp_path) -> bytes:
    kp = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-q",
                    "-f", str(kp)], check=True)
    return kp.read_bytes()


class TestChannelPublication:
    def test_env_without_command_publishes_nothing(self, tmp_path):
        # The env method's only reference is the plaintext value, so it
        # must never be published into the run context.
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        engine = WorkflowEngine(audit_logger=audit,
                                secret_source=_FakeSource(SECRET))
        engine._workflows["wf"] = WorkflowDef(
            name="wf", trigger=TriggerDef(type=TriggerType.CRON),
            needs=["vault/dev/key"],
            steps=[StepDef(type=StepType.DELIVER_SECRET, config={
                "item": "vault/dev/key", "as": "env", "var": "X"})])
        run = engine.start_run("wf")
        assert run.state == RunState.COMPLETED
        assert "channel_env" not in run.context
        assert SECRET.decode() not in str(run.context)
        assert "published" not in run.steps_completed[0].details


@pytest.mark.skipif(not _HAVE_SSH, reason="ssh tooling not available")
class TestSshAgentConsumptionLoop:
    def _wf(self, runtime, *, consumer_cmd=None, consume=True,
            fail_after=False):
        deliver = StepDef(type=StepType.DELIVER_SECRET, name="deliver", config={
            "item": "vault/dev/github-ssh-key", "as": "ssh-agent",
            "runtime_root": str(runtime), "ttl": 60,
            "expose_as": "SSH_AUTH_SOCK", "scrub_on": "workflow_exit"})
        steps = [deliver]
        if consumer_cmd is not None:
            cfg = {"item": "vault/dev/marker", "as": "env",
                   "var": "MARKER", "command": consumer_cmd}
            if consume:
                cfg["consume_channels"] = ["SSH_AUTH_SOCK"]
            steps.append(StepDef(type=StepType.DELIVER_SECRET, name="consume",
                                 config=cfg))
        if fail_after:
            steps.append(StepDef(type=StepType.RUN_HOOK, name="boom",
                                 config={"hook": "explode"}))
        return WorkflowDef(name="git-sign",
                           trigger=TriggerDef(type=TriggerType.PROCESS_SPAWN),
                           needs=["vault/dev/github-ssh-key",
                                  "vault/dev/marker"],
                           steps=steps)

    def _engine(self, tmp_path, key):
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        return WorkflowEngine(audit_logger=audit, broker_proxy=_AllowBroker(),
                              secret_source=_FakeSource(key)), audit

    def test_socket_published_then_scrubbed(self, tmp_path):
        key = _gen_ssh_key(tmp_path)
        runtime = tmp_path / "rt"
        engine, _audit = self._engine(tmp_path, key)
        engine._workflows["git-sign"] = self._wf(runtime)

        run = engine.start_run("git-sign")
        assert run.state == RunState.COMPLETED, run.error
        # The deliver step published the agent socket reference...
        assert run.steps_completed[0].details["published"] == ["SSH_AUTH_SOCK"]
        # ...but never the key material.
        assert key.decode("latin1", "ignore")[:20] not in str(run.context)
        # After workflow exit the socket is scrubbed (gone) and the agent
        # dir was removed, leaving no per-run state behind.
        sock = run.context["channel_env"]["SSH_AUTH_SOCK"]
        assert not os.path.exists(sock)
        assert not os.path.isdir(runtime) or os.listdir(runtime) == []

    def test_published_socket_consumed_by_later_step(self, tmp_path):
        key = _gen_ssh_key(tmp_path)
        runtime = tmp_path / "rt"
        proof = tmp_path / "proof"
        # A consumer command that proves it inherited the published
        # SSH_AUTH_SOCK and the agent really holds the key.
        consumer = [sys.executable, "-c",
                    "import os,subprocess,sys;"
                    "sock=os.environ.get('SSH_AUTH_SOCK','');"
                    "r=subprocess.run(['ssh-add','-l'],"
                    "  env=dict(os.environ,SSH_AUTH_SOCK=sock),"
                    "  capture_output=True);"
                    f"open({str(proof)!r},'w').write("
                    "sock+'\\n'+('OK' if r.returncode==0 else 'NOKEY'))"]
        engine, _audit = self._engine(tmp_path, key)
        engine._workflows["git-sign"] = self._wf(runtime, consumer_cmd=consumer)

        run = engine.start_run("git-sign")
        assert run.state == RunState.COMPLETED, run.error
        lines = proof.read_text().splitlines()
        # The consumer saw a non-empty socket path and the agent had a key
        # loaded (proving real end-to-end delivery + consumption).
        assert lines[0]  # SSH_AUTH_SOCK was set in the child env
        assert lines[1] == "OK"
        # ...and after the run the socket is gone (scrubbed).
        assert not os.path.exists(lines[0])

    def test_channel_not_inherited_without_opt_in(self, tmp_path):
        # Least privilege: a later command-bearing step that does NOT list
        # consume_channels must not inherit an earlier SSH_AUTH_SOCK.
        key = _gen_ssh_key(tmp_path)
        runtime = tmp_path / "rt"
        proof = tmp_path / "proof"
        consumer = [sys.executable, "-c",
                    f"import os;open({str(proof)!r},'w').write("
                    "os.environ.get('SSH_AUTH_SOCK','<unset>'))"]
        engine, _audit = self._engine(tmp_path, key)
        engine._workflows["git-sign"] = self._wf(
            runtime, consumer_cmd=consumer, consume=False)

        run = engine.start_run("git-sign")
        assert run.state == RunState.COMPLETED, run.error
        # The opt-out child never received the run's published channel
        # (it may see the host's own SSH_AUTH_SOCK, but never this run's).
        published = run.context["channel_env"]["SSH_AUTH_SOCK"]
        assert proof.read_text() != published

    def test_failure_after_publish_scrubs_socket(self, tmp_path):
        key = _gen_ssh_key(tmp_path)
        runtime = tmp_path / "rt"
        engine, _audit = self._engine(tmp_path, key)
        engine._workflows["git-sign"] = self._wf(runtime, fail_after=True)

        # Force the trailing hook step to fail.
        def boom(step, run, result, wf=None):
            result.success = False
            result.error = "kaboom"
        engine._handle_run_hook = boom  # type: ignore[assignment]

        run = engine.start_run("git-sign")
        assert run.state == RunState.FAILED
        sock = run.context["channel_env"]["SSH_AUTH_SOCK"]
        # Mid-step failure still scrubs the published channel.
        assert not os.path.exists(sock)
        assert run.run_id not in engine._delivery_handles


# ======================================================================
# External git-sign bridge (qsu --workflow-run -> root-exec channel_env)
# ======================================================================

import qdistro_root_exec as REX  # noqa: E402
from workflow_schema import WorkflowRun  # noqa: E402


def _running_run_with_channel(engine, env):
    """Register a RUNNING run on the engine whose context already publishes
    ``env`` on channel_env (mirrors what _publish_channel does after a real
    deliver_secret step)."""
    run = WorkflowRun(workflow_name="git-sign", trigger_context={})
    run.mark_running()
    run.context["channel_env"] = dict(env)
    with engine._runs_lock:
        engine._runs[run.run_id] = run
    return run


class TestEngineGetRunChannelEnv:
    """Read side of the bridge — the engine surface GetRunChannelEnv calls."""

    def _engine(self, tmp_path):
        audit = WorkflowAuditLogger(db_path=str(tmp_path / "a.sqlite"))
        return WorkflowEngine(audit_logger=audit)

    def test_running_run_returns_allowlisted(self, tmp_path):
        engine = self._engine(tmp_path)
        run = _running_run_with_channel(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock"})
        out = engine.get_run_channel_env(run.run_id)
        assert out == {"SSH_AUTH_SOCK": "/run/agent.sock"}

    def test_unknown_run_returns_empty(self, tmp_path):
        engine = self._engine(tmp_path)
        assert engine.get_run_channel_env("does-not-exist") == {}

    def test_non_allowlisted_name_filtered_out(self, tmp_path):
        engine = self._engine(tmp_path)
        # A run that somehow published a non-allowlisted key must never
        # leak it to an external child.
        run = _running_run_with_channel(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock",
                     "SECRET_TOKEN": "p@ss"})
        out = engine.get_run_channel_env(run.run_id)
        assert out == {"SSH_AUTH_SOCK": "/run/agent.sock"}
        assert "SECRET_TOKEN" not in out

    def test_explicit_name_intersects_allowlist(self, tmp_path):
        engine = self._engine(tmp_path)
        run = _running_run_with_channel(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock"})
        # Asking for a non-allowlisted name returns nothing for it.
        assert engine.get_run_channel_env(run.run_id, ["SECRET_TOKEN"]) == {}
        assert engine.get_run_channel_env(
            run.run_id, ["SSH_AUTH_SOCK"]) == {"SSH_AUTH_SOCK": "/run/agent.sock"}

    def test_completed_run_returns_empty(self, tmp_path):
        # A non-RUNNING run's socket has already been scrubbed; its
        # channel_env entry is a dangling path -> must not be handed out.
        engine = self._engine(tmp_path)
        run = _running_run_with_channel(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock"})
        run.mark_completed()
        assert engine.get_run_channel_env(run.run_id) == {}

    def test_not_yet_published_returns_empty(self, tmp_path):
        engine = self._engine(tmp_path)
        run = WorkflowRun(workflow_name="git-sign", trigger_context={})
        run.mark_running()
        with engine._runs_lock:
            engine._runs[run.run_id] = run
        assert engine.get_run_channel_env(run.run_id) == {}

    def test_wait_returns_when_published_late(self, tmp_path):
        # The publish lands after the first poll -> the bounded wait picks
        # it up rather than timing out.
        engine = self._engine(tmp_path)
        run = WorkflowRun(workflow_name="git-sign", trigger_context={})
        run.mark_running()
        with engine._runs_lock:
            engine._runs[run.run_id] = run

        import threading
        def publish_later():
            time.sleep(0.15)
            run.context["channel_env"] = {"SSH_AUTH_SOCK": "/run/late.sock"}
        threading.Thread(target=publish_later, daemon=True).start()
        out = engine.wait_for_run_channel_env(
            run.run_id, timeout=2.0, poll_interval=0.02)
        assert out == {"SSH_AUTH_SOCK": "/run/late.sock"}

    def test_wait_times_out_fail_closed(self, tmp_path):
        engine = self._engine(tmp_path)
        run = WorkflowRun(workflow_name="git-sign", trigger_context={})
        run.mark_running()
        with engine._runs_lock:
            engine._runs[run.run_id] = run
        # Never publishes -> bounded wait returns {} (fail closed).
        out = engine.wait_for_run_channel_env(
            run.run_id, timeout=0.2, poll_interval=0.02)
        assert out == {}

    # --- caller-uid binding (run_id is not a bearer token) --------------

    def _running_run_with_trigger_pid(self, engine, env, pid):
        run = WorkflowRun(
            workflow_name="git-sign",
            trigger_context={"pid": pid})
        run.mark_running()
        run.context["channel_env"] = dict(env)
        with engine._runs_lock:
            engine._runs[run.run_id] = run
        return run

    def test_uid_binding_matches_caller(self, tmp_path):
        # The triggering pid is THIS process; caller_uid == our uid matches.
        engine = self._engine(tmp_path)
        run = self._running_run_with_trigger_pid(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock"}, os.getpid())
        out = engine.get_run_channel_env(run.run_id, caller_uid=os.getuid())
        assert out == {"SSH_AUTH_SOCK": "/run/agent.sock"}

    def test_uid_binding_rejects_other_uid(self, tmp_path):
        # A different uid (run_id leaked via the pending broadcast) cannot
        # pull the socket — the triggering process is owned by us, not them.
        engine = self._engine(tmp_path)
        run = self._running_run_with_trigger_pid(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock"}, os.getpid())
        other = os.getuid() + 12345
        assert engine.get_run_channel_env(run.run_id, caller_uid=other) == {}

    def test_uid_binding_no_trigger_pid_fails_closed(self, tmp_path):
        # A cron/engine-launched run has no triggering process to bind to,
        # so the uid-bound external path can never reach it.
        engine = self._engine(tmp_path)
        run = _running_run_with_channel(
            engine, {"SSH_AUTH_SOCK": "/run/agent.sock"})
        assert engine.get_run_channel_env(
            run.run_id, caller_uid=os.getuid()) == {}


class TestRootExecResolveChannelEnv:
    """root-exec side — bounded broker poll + own-allowlist re-filter.

    The injected lookups take ``*a`` because the real signature is
    ``lookup(run_id, names, caller_uid, call_timeout)``; the daemon
    forwards the qsu caller uid (1234 here) so the engine can bind the run.
    """

    def test_success_folds_value(self):
        calls = []
        def lookup(run_id, names, *a):
            calls.append((run_id, tuple(names), a))
            return {"SSH_AUTH_SOCK": "/run/agent.sock"}
        out = REX._resolve_channel_env("run-abc-123", 1234, _lookup=lookup)
        assert out == {"SSH_AUTH_SOCK": "/run/agent.sock"}
        # It only ever asked for allowlisted names...
        assert calls[0][1] == ("SSH_AUTH_SOCK",)
        # ...and forwarded the caller uid for binding.
        assert calls[0][2][0] == 1234

    def test_invalid_run_id_fails_closed(self):
        for bad in ("", "bad id with spaces", "x" * 65, "a;b", "../etc"):
            with pytest.raises(REX.ChannelEnvUnavailable):
                REX._resolve_channel_env(bad, 1234, _lookup=lambda *a: {})

    def test_unknown_run_fails_closed(self):
        # Broker keeps returning {} (run unknown / never published / uid
        # mismatch) -> bounded wait elapses -> ChannelEnvUnavailable.
        with pytest.raises(REX.ChannelEnvUnavailable):
            REX._resolve_channel_env(
                "run-unknown", 1234, wait_s=0.1, poll_s=0.02,
                _lookup=lambda *a: {})

    def test_not_yet_published_then_succeeds(self):
        seq = [{}, {}, {"SSH_AUTH_SOCK": "/run/agent.sock"}]
        def lookup(run_id, names, *a):
            return seq.pop(0) if seq else {"SSH_AUTH_SOCK": "/run/agent.sock"}
        out = REX._resolve_channel_env(
            "run-late", 1234, wait_s=2.0, poll_s=0.01, _lookup=lookup)
        assert out == {"SSH_AUTH_SOCK": "/run/agent.sock"}

    def test_non_allowlisted_name_from_broker_rejected(self):
        # Defense in depth: even if the broker returned a non-allowlisted
        # name, the daemon refuses (allowlist drift -> fail closed).
        def lookup(run_id, names, *a):
            return {"EVIL_VAR": "value"}
        with pytest.raises(REX.ChannelEnvUnavailable):
            REX._resolve_channel_env("run-evil", 1234, wait_s=0.1, poll_s=0.02,
                                     _lookup=lookup)

    def test_empty_value_rejected(self):
        def lookup(run_id, names, *a):
            return {"SSH_AUTH_SOCK": ""}
        # Empty value never satisfies the wait (treated as not-published) ->
        # times out fail-closed.
        with pytest.raises(REX.ChannelEnvUnavailable):
            REX._resolve_channel_env("run-empty", 1234, wait_s=0.1, poll_s=0.02,
                                     _lookup=lookup)

    def test_broker_transport_error_fails_closed(self):
        def lookup(run_id, names, *a):
            raise RuntimeError("dbus down")
        with pytest.raises(REX.ChannelEnvUnavailable):
            REX._resolve_channel_env("run-down", 1234, wait_s=0.1, poll_s=0.02,
                                     _lookup=lookup)

    def test_no_lookup_started_after_deadline(self):
        # The total wait must be bounded: once the deadline has passed the
        # loop must NOT start another (potentially blocking) lookup. Drive a
        # virtual clock and assert the per-call timeout never exceeds the
        # remaining budget and no call is issued past the deadline.
        now = [0.0]
        timeouts = []
        def clock():
            return now[0]
        def sleep(dt):
            now[0] += dt  # advance the virtual clock by the poll interval
        def lookup(run_id, names, caller_uid, call_timeout):
            # Never start a call once the deadline has elapsed.
            assert now[0] < 1.0, "lookup started at/after the deadline"
            # The per-call timeout never exceeds the remaining budget.
            assert call_timeout <= (1.0 - now[0]) + 1e-9
            timeouts.append(call_timeout)
            return {}
        with pytest.raises(REX.ChannelEnvUnavailable):
            REX._resolve_channel_env(
                "run-x", 1234, wait_s=1.0, poll_s=0.3,
                _clock=clock, _sleep=sleep, _lookup=lookup)
        assert timeouts  # at least one probe happened


class TestRootExecChildEnvFold:
    """The env the daemon hands the child: SSH_AUTH_SOCK folded in, but
    only allowlisted names. We capture the env passed to subprocess.Popen
    rather than actually dropping privileges (the real path needs CAP_SETGID
    to setgid/setgroups for the target user — exercised in the VM smoke).
    """

    def _capture_env(self, monkeypatch, channel_env):
        import subprocess as _sp
        captured = {}

        class _FakeProc:
            returncode = 0
            def __init__(self, *a, **kw):
                captured["env"] = kw.get("env")
                # Provide closed-ish pipe stand-ins so the stream loop ends.
                r1, w1 = os.pipe(); r2, w2 = os.pipe()
                os.close(w1); os.close(w2)
                self.stdout = os.fdopen(r1, "rb")
                self.stderr = os.fdopen(r2, "rb")
            def poll(self):
                return 0
            def wait(self):
                return 0

        monkeypatch.setattr(_sp, "Popen", _FakeProc)
        # Avoid NSS/getgrouplist needing a real user; resolve to current.
        monkeypatch.setattr(REX, "_resolve_target",
                            lambda u: (os.getuid(), os.getgid(),
                                       "/tmp", "/bin/sh"))
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            REX._spawn_and_stream(a, "someuser", ["/bin/true"],
                                  channel_env=channel_env)
        finally:
            a.close(); b.close()
        return captured["env"]

    def test_child_inherits_channel_env(self, monkeypatch):
        env = self._capture_env(
            monkeypatch, {"SSH_AUTH_SOCK": "/run/agent.sock"})
        assert env.get("SSH_AUTH_SOCK") == "/run/agent.sock"

    def test_non_allowlisted_channel_env_not_folded(self, monkeypatch):
        # Even if a non-allowlisted name reaches _spawn_and_stream, the
        # final re-assert drops it (defense in depth).
        env = self._capture_env(monkeypatch, {"EVIL_VAR": "value"})
        assert "EVIL_VAR" not in env

    def test_no_channel_env_is_clean(self, monkeypatch):
        env = self._capture_env(monkeypatch, None)
        assert "SSH_AUTH_SOCK" not in env
