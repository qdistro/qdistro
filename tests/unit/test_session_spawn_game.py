"""Tests for games/qdistro_session_spawn_game — spec/12 dry-run probe.

Pure-python config-renderer; the real chvt + systemd-run path is
out-of-scope. Tests cover validation, command rendering for both
cage + gamescope, and the dry-run write-to-tmpdir helper.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_MOD = (Path(__file__).resolve().parent.parent
        / "games" / "qdistro_session_spawn_game.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_session_spawn_game", _MOD)
g = importlib.util.module_from_spec(spec)
sys.modules["qdistro_session_spawn_game"] = g
spec.loader.exec_module(g)


# ---- input validation ----

class TestValidation:
    def test_bad_user_rejected(self):
        for bad in ("", "Root", "1user", "user with space", "u" * 33):
            try:
                g._validate_user(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"user {bad!r} should have failed")

    def test_good_user(self):
        for ok in ("games-user", "u1", "a_b"):
            assert g._validate_user(ok) == ok

    def test_bad_vt(self):
        for bad in (0, 3, 13, "4", -1):
            try:
                g._validate_vt(bad)  # type: ignore
            except ValueError:
                pass
            else:
                raise AssertionError(f"vt {bad!r} should have failed")

    def test_good_vt(self):
        for ok in (4, 5, 12):
            assert g._validate_vt(ok) == ok

    def test_bad_launcher(self):
        try:
            g._validate_launcher("weston")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# ---- command rendering ----

class TestRenderCommand:
    def test_cage_basic(self):
        cmd = g.render_command("cage", ["steam"])
        assert cmd == "/usr/bin/cage -s -- steam"

    def test_cage_with_complex_arg(self):
        cmd = g.render_command(
            "cage", ["/usr/bin/firefox", "--kiosk", "https://example.com"])
        assert "/usr/bin/cage -s --" in cmd
        assert "/usr/bin/firefox" in cmd
        assert "--kiosk" in cmd

    def test_gamescope_basic(self):
        cmd = g.render_command("gamescope", ["steam"])
        # gamescope does NOT get -s
        assert cmd == "/usr/bin/gamescope -- steam"

    def test_argv_with_space_is_quoted(self):
        cmd = g.render_command(
            "cage", ["/opt/Some Game/run.sh"])
        # shlex.quote wraps the path in single-quotes since it
        # contains a space.
        assert "'/opt/Some Game/run.sh'" in cmd

    def test_empty_argv_allowed_in_render(self):
        cmd = g.render_command("cage", [])
        assert cmd.endswith("--")


# ---- TOML rendering ----

class TestRenderConfig:
    def test_shape(self):
        body = g.render_greetd_config(
            user="games-user", vt=4, launcher="cage",
            argv=["steam"])
        assert "[terminal]" in body
        assert "vt = 4" in body
        assert "switch = false" in body
        assert "[default_session]" in body
        assert "user = games-user" in body
        assert "command = \"/usr/bin/cage -s -- steam\"" in body

    def test_with_gamescope(self):
        body = g.render_greetd_config(
            user="games-user", vt=5, launcher="gamescope",
            argv=["steam", "-fulldesktop"])
        assert "vt = 5" in body
        assert "/usr/bin/gamescope --" in body
        assert "-fulldesktop" in body

    def test_invalid_inputs_raise(self):
        try:
            g.render_greetd_config(
                user="root!", vt=4, launcher="cage", argv=[])
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# ---- dry-run write ----

class TestDryRunWrite:
    def test_write_creates_file(self, tmp_path):
        root = tmp_path / "greetd"
        path, body = g.write_config_dry_run(
            user="games-user", vt=4, launcher="cage",
            argv=["steam"], root=root)
        assert path.endswith("qdistro-game-4.toml")
        assert os.path.isfile(path)
        with open(path, "r") as f:
            disk = f.read()
        assert disk == body
        assert "vt = 4" in disk
        st = os.stat(path)
        # 0o600 expected
        assert (st.st_mode & 0o777) == 0o600

    def test_write_preserves_argv_quoting(self, tmp_path):
        path, body = g.write_config_dry_run(
            user="games-user", vt=6, launcher="cage",
            argv=["/opt/a b/c"], root=tmp_path)
        assert "'/opt/a b/c'" in body

    def test_path_outside_run_greetd_works_for_tests(self, tmp_path):
        # The spec's real path is /run/greetd/qdistro-game-<vt>.toml
        # but tests override root for sandbox isolation.
        path = g.config_path(4, root=str(tmp_path))
        assert path.startswith(str(tmp_path))
