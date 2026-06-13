"""Tests for the SDK workflow-runner consumer (07-disposables-plan P2b
§Lifecycle "workflow step completed").

The disposable teardown surface (``DisposeByWorkflow`` on SessionManager1) is
SHIPPED + VM-proven; this consumer is the missing glue: a ``WorkflowRun`` /
``WorkflowStep`` runner that tags step disposables into a workflow group at spawn
and calls ``DisposeByWorkflow(id)`` on step completion / context exit (including
exception). These tests pin, against a FAKE system bus + a FAKE spawn binary:

  - the workflow id is regex-validated client-side (an invalid id would spawn an
    UNTAGGED — unreapable — disposable, a silent leak spawn-tier2.sh can't catch);
  - the id is propagated to the spawn opt-in env (``QDISTRO_DISPOSABLE_WORKFLOW``)
    and the runner OWNS it (a caller's extra_env cannot override it);
  - ``DisposeByWorkflow`` is called with the right id on normal exit AND on an
    exception unwind (without masking the user's exception);
  - teardown is idempotent / no-throw on the context-exit path, but the EXPLICIT
    dispose surface PROPAGATES daemon errors;
  - per-step grouping uses distinct ids and the parent exit sweeps every step.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

pytest.importorskip("dbus")
import dbus  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
_SDK = REPO_ROOT / "sdk"
sys.path.insert(0, str(_SDK))
import qdistro_app  # noqa: E402

RESOLVER = REPO_ROOT / "session_manager" / "qdistro_disposable_classes.py"
REGISTRY = REPO_ROOT / "session_manager" / "disposable-classes.toml"


# --- fakes ------------------------------------------------------------------

class _FakeIfaceProxy:
    """Stands in for the dbus object proxy: records DisposeByWorkflow calls and
    optionally raises to simulate a daemon fail-closed refusal."""

    def __init__(self, recorder: _FakeBus):
        self._rec = recorder

    def DisposeByWorkflow(self, workflow_id, timeout=None):  # noqa: N802
        self._rec.calls.append(str(workflow_id))
        self._rec.timeouts.append(timeout)
        if self._rec.raise_on and str(workflow_id) in self._rec.raise_on:
            raise dbus.DBusException(
                "org.qdistro.SessionManager1.BadState: partial teardown")
        return self._rec.returns.get(str(workflow_id), 1)


class _FakeBus:
    """A fake system bus: ``get_object`` hands back a proxy that records every
    ``DisposeByWorkflow`` call. ``calls`` is the ordered id log."""

    def __init__(self):
        self.calls: list[str] = []
        self.timeouts: list[float | None] = []
        self.returns: dict[str, int] = {}
        self.raise_on: set[str] = set()
        self.requested: list[tuple[str, str]] = []

    def get_object(self, bus_name, obj_path):
        self.requested.append((bus_name, obj_path))
        return self._proxy

    @property
    def _proxy(self):
        return _FakeIfaceProxy(self)


@pytest.fixture
def fakebus(monkeypatch):
    bus = _FakeBus()
    # dbus.Interface(proxy, iface) must return something with DisposeByWorkflow;
    # our proxy already exposes it, so make Interface a pass-through.
    monkeypatch.setattr(qdistro_app.dbus, "Interface",
                        lambda obj, iface: obj)
    # Default seam: dispose_workflow() with no bus= uses _session_bus().
    monkeypatch.setattr(qdistro_app, "_session_bus", lambda: bus)
    return bus


def _fake_spawn_bin(tmp_path: Path, *, rc: int = 0, stdout: str = "",
                    stderr: str = "") -> Path:
    """A fake qdistro-tier2-spawn recording argv+env, emitting a canned
    contract. Mirrors test_open_in_disposable_sdk's helper."""
    bin_path = tmp_path / "qdistro-tier2-spawn"
    argv_file = tmp_path / "spawn-argv"
    env_file = tmp_path / "spawn-env"
    bin_path.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'env > "{env_file}"\n'
        f'printf "%s" "{stdout}"\n'
        f'printf "%s" "{stderr}" >&2\n'
        f"exit {rc}\n"
    )
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return bin_path


def _base_env() -> dict[str, str]:
    return {
        "QDISTRO_DISPOSABLE_CLASSES_RESOLVER": str(RESOLVER),
        "QDISTRO_DISPOSABLE_CLASSES": str(REGISTRY),
    }


def _read_env(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "spawn-env").read_text().splitlines()
    return dict(ln.split("=", 1) for ln in lines if "=" in ln)


# --- id validation / generation ---------------------------------------------

def test_generate_workflow_id_is_regex_valid():
    wid = qdistro_app.generate_workflow_id()
    assert qdistro_app._WORKFLOW_ID_RE.fullmatch(wid)
    assert wid.startswith("wf-")
    # Two mints don't collide.
    assert qdistro_app.generate_workflow_id() != qdistro_app.generate_workflow_id()


def test_generate_workflow_id_custom_prefix():
    wid = qdistro_app.generate_workflow_id("etl")
    assert wid.startswith("etl-")
    assert qdistro_app._WORKFLOW_ID_RE.fullmatch(wid)


@pytest.mark.parametrize("bad", ["UPPER", "", "-lead", "has space",
                                 "x;rm", "x" * 130])
def test_generate_workflow_id_rejects_bad_prefix(bad):
    with pytest.raises(ValueError):
        qdistro_app.generate_workflow_id(bad)


@pytest.mark.parametrize("bad", ["UPPER", "", "has space", "x;rm -rf /",
                                 "-lead", "x" * 129, 123])
def test_workflow_run_rejects_invalid_id(bad):
    with pytest.raises(ValueError):
        qdistro_app.WorkflowRun(bad)


def test_workflow_run_none_mints_fresh_id():
    """``WorkflowRun(None)`` (the default) mints a fresh valid id, it does NOT
    reject — None is the 'please generate one' sentinel."""
    wf = qdistro_app.WorkflowRun()
    assert qdistro_app._WORKFLOW_ID_RE.fullmatch(wf.id)
    wf2 = qdistro_app.WorkflowRun(None)
    assert qdistro_app._WORKFLOW_ID_RE.fullmatch(wf2.id)
    assert wf.id != wf2.id


# --- dispose_workflow (explicit surface PROPAGATES) -------------------------

def test_dispose_workflow_calls_session_manager(fakebus):
    fakebus.returns["step-1"] = 3
    n = qdistro_app.dispose_workflow("step-1")
    assert n == 3
    assert fakebus.calls == ["step-1"]
    # Pinned to SessionManager1, NOT the AdminBroker.
    assert fakebus.requested == [("org.qdistro.SessionManager1",
                                  "/org/qdistro/SessionManager1")]


def test_dispose_workflow_rejects_invalid_id_before_wire(fakebus):
    with pytest.raises(ValueError):
        qdistro_app.dispose_workflow("BAD ID")
    assert fakebus.calls == []  # never reached the bus


def test_dispose_workflow_propagates_daemon_error(fakebus):
    fakebus.raise_on = {"step-1"}
    with pytest.raises(dbus.DBusException):
        qdistro_app.dispose_workflow("step-1")
    assert fakebus.calls == ["step-1"]


def test_dispose_workflow_accepts_injected_bus():
    """The ``bus=`` seam lets a caller inject the connection — used by
    WorkflowRun to thread a test bus through. (Patch dbus.Interface to a
    pass-through so our fake proxy is used verbatim.)"""
    bus = _FakeBus()
    bus.returns["wf-deadbeef"] = 2
    import unittest.mock as mock
    with mock.patch.object(qdistro_app.dbus, "Interface",
                           lambda obj, iface: obj):
        n = qdistro_app.dispose_workflow("wf-deadbeef", bus=bus)
    assert n == 2
    assert bus.calls == ["wf-deadbeef"]


# --- WorkflowRun: tagging + propagation -------------------------------------

def test_open_in_disposable_tags_workflow_env(fakebus, tmp_path):
    spawn = _fake_spawn_bin(tmp_path, stdout="CONTAINER=disp-x\n")
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    wf.open_in_disposable(_mkfile(tmp_path), class_name="agent-scratch",
                          spawn_bin=str(spawn), extra_env=_base_env())
    env = _read_env(tmp_path)
    assert env[qdistro_app.WORKFLOW_ENV] == "etl-run"


def test_runner_owns_tag_caller_cannot_override(fakebus, tmp_path):
    """A caller's extra_env attempting to set the workflow env is overridden by
    the runner-owned id — never the other way (an override would strand an
    untagged / mis-tagged disposable)."""
    spawn = _fake_spawn_bin(tmp_path, stdout="CONTAINER=disp-x\n")
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    hostile = dict(_base_env())
    hostile[qdistro_app.WORKFLOW_ENV] = "attacker-id"
    wf.open_in_disposable(_mkfile(tmp_path), class_name="agent-scratch",
                          spawn_bin=str(spawn), extra_env=hostile)
    env = _read_env(tmp_path)
    assert env[qdistro_app.WORKFLOW_ENV] == "etl-run"


def test_open_for_edit_tags_and_sets_edit_env(fakebus, tmp_path):
    spawn = _fake_spawn_bin(tmp_path, stdout="CONTAINER=disp-x\n")
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    wf.open_for_edit(_mkfile(tmp_path), class_name="agent-scratch",
                     request_silo="work", spawn_bin=str(spawn),
                     extra_env=_base_env())
    env = _read_env(tmp_path)
    assert env[qdistro_app.WORKFLOW_ENV] == "etl-run"
    assert env["TIER2_REQUEST_EDIT"] == "1"
    assert env["TIER2_REQUEST_SILO"] == "work"


def test_open_for_edit_runner_owns_tag_caller_cannot_override(fakebus, tmp_path):
    """The edit path also wins the workflow tag over a hostile extra_env (the
    edit fields + the caller env are layered, but the runner-owned id is stamped
    last) — an override here would strand an untagged disposable just like the
    plain open path."""
    spawn = _fake_spawn_bin(tmp_path, stdout="CONTAINER=disp-x\n")
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    hostile = dict(_base_env())
    hostile[qdistro_app.WORKFLOW_ENV] = "attacker-id"
    wf.open_for_edit(_mkfile(tmp_path), class_name="agent-scratch",
                     request_silo="work", spawn_bin=str(spawn),
                     extra_env=hostile)
    env = _read_env(tmp_path)
    assert env[qdistro_app.WORKFLOW_ENV] == "etl-run"
    assert env["TIER2_REQUEST_EDIT"] == "1"


# --- WorkflowRun: teardown on exit (normal + exception) ---------------------

def test_context_exit_disposes_group(fakebus):
    with qdistro_app.WorkflowRun("etl-run", bus=fakebus) as wf:
        assert wf.id == "etl-run"
    assert fakebus.calls == ["etl-run"]
    # Cleanup path uses the SHORT timeout (not the 600s default) so a wedged
    # daemon can't block exit for the full default per group.
    assert fakebus.timeouts == [qdistro_app.CLEANUP_TIMEOUT_S]


def test_explicit_dispose_uses_default_timeout(fakebus):
    """The explicit surface uses the full default reply timeout (the caller
    blocks deliberately), distinct from the short cleanup-path timeout."""
    qdistro_app.WorkflowRun("etl-run", bus=fakebus).dispose()
    assert fakebus.timeouts == [qdistro_app.DEFAULT_TIMEOUT_S]


def test_context_exit_disposes_on_exception_without_masking(fakebus):
    class Boom(RuntimeError):
        pass
    with pytest.raises(Boom):
        with qdistro_app.WorkflowRun("etl-run", bus=fakebus):
            raise Boom("user error")
    # Teardown ran even though the body raised, and the user's exception
    # propagated (the with-block re-raised Boom, not a teardown error).
    assert fakebus.calls == ["etl-run"]


def test_context_exit_swallows_teardown_error(fakebus):
    """A daemon fail-closed on the cleanup path must NOT raise out of __exit__
    (it would mask any in-flight exception / break the with-statement)."""
    fakebus.raise_on = {"etl-run"}
    # No exception in the body — exit's swallowed teardown error must not surface.
    with qdistro_app.WorkflowRun("etl-run", bus=fakebus):
        pass
    assert fakebus.calls == ["etl-run"]


def test_context_exit_swallows_teardown_error_during_exception(fakebus):
    fakebus.raise_on = {"etl-run"}

    class Boom(RuntimeError):
        pass
    # The user's Boom must propagate, NOT the swallowed teardown DBusException.
    with pytest.raises(Boom):
        with qdistro_app.WorkflowRun("etl-run", bus=fakebus):
            raise Boom("user")


def test_explicit_dispose_propagates(fakebus):
    fakebus.raise_on = {"etl-run"}
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    with pytest.raises(dbus.DBusException):
        wf.dispose()


def test_dispose_is_idempotent_zero(fakebus):
    fakebus.returns["etl-run"] = 0  # daemon: nothing carried the id
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    assert wf.dispose() == 0


# --- steps ------------------------------------------------------------------

def test_step_uses_distinct_id_and_sweeps_on_exit(fakebus):
    with qdistro_app.WorkflowRun("etl-run", bus=fakebus) as wf:
        with wf.step("fetch") as st:
            assert st.id != wf.id
            assert st.id.startswith("etl-run-s1")
        # step exit disposed the step group
        assert fakebus.calls == [st.id]
    # run exit then sweeps every step id it minted AND its own group, idempotently
    assert fakebus.calls == [st.id, st.id, "etl-run"]


def test_step_disposed_on_exception_then_parent_sweeps(fakebus):
    class Boom(RuntimeError):
        pass
    captured = {}
    with pytest.raises(Boom):
        with qdistro_app.WorkflowRun("etl-run", bus=fakebus) as wf:
            st = wf.step()
            captured["sid"] = st.id
            with st:
                raise Boom("inside step")
    # step exit (during exception) disposed the step group; parent exit (also
    # during exception) swept the step id again + its own group.
    sid = captured["sid"]
    assert fakebus.calls == [sid, sid, "etl-run"]


def test_step_ids_are_regex_valid_even_with_long_parent(fakebus):
    long_id = "p" + "a" * 126  # 127 chars, valid
    wf = qdistro_app.WorkflowRun(long_id, bus=fakebus)
    st = wf.step("a-very-long-step-name-that-would-overflow-the-cap")
    assert qdistro_app._WORKFLOW_ID_RE.fullmatch(st.id)
    assert len(st.id) <= 128


def test_step_name_sanitized(fakebus):
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    st = wf.step("Fetch URLs!")  # uppercase + space + punctuation
    assert qdistro_app._WORKFLOW_ID_RE.fullmatch(st.id)


def test_step_explicit_dispose_propagates(fakebus):
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    st = wf.step()
    fakebus.raise_on = {st.id}
    with pytest.raises(dbus.DBusException):
        st.dispose()


def test_step_open_tags_step_id_not_run_id(fakebus, tmp_path):
    spawn = _fake_spawn_bin(tmp_path, stdout="CONTAINER=disp-x\n")
    wf = qdistro_app.WorkflowRun("etl-run", bus=fakebus)
    st = wf.step("fetch")
    st.open_in_disposable(_mkfile(tmp_path), class_name="agent-scratch",
                          spawn_bin=str(spawn), extra_env=_base_env())
    env = _read_env(tmp_path)
    # The disposable carries the STEP id, not the run id (one label per
    # container) — which is why the parent exit must sweep every step id.
    assert env[qdistro_app.WORKFLOW_ENV] == st.id
    assert env[qdistro_app.WORKFLOW_ENV] != wf.id


def test_spawn_failed_midlaunch_still_reaped_by_group(fakebus, tmp_path):
    """open_in_disposable raises if the spawn binary errors, but the by-label
    group teardown the run performs on exit still reaps any container the binary
    created before failing — the whole reason group-by-label is the primitive."""
    spawn = _fake_spawn_bin(tmp_path, rc=2, stderr="broker denied")
    with pytest.raises(qdistro_app.OpenInDisposableError):
        with qdistro_app.WorkflowRun("etl-run", bus=fakebus) as wf:
            wf.open_in_disposable(_mkfile(tmp_path), class_name="agent-scratch",
                                  spawn_bin=str(spawn), extra_env=_base_env())
    # Exit still tore the group down by label.
    assert fakebus.calls == ["etl-run"]


def _mkfile(tmp_path: Path) -> str:
    f = tmp_path / "in.txt"
    f.write_text("hi")
    return str(f)
