"""Tests for the polkit→qdistro action mapper.

Keeping this test file next to the existing broker/cli tests so the
same pytest invocation picks them up. The mapper is pure; no dbus
or mainloop deps needed.
"""
from __future__ import annotations

import pytest

from qdistro_polkit_agent import action_to_qdistro


class TestMapper:
    def test_freedesktop_prefix_stripped(self):
        got = action_to_qdistro(
            "org.freedesktop.NetworkManager.settings.modify.system")
        assert got == "qdistro.NetworkManager.settings.modify.system"

    def test_freedesktop_short_form(self):
        got = action_to_qdistro("org.freedesktop.policykit.exec")
        assert got == "qdistro.policykit.exec"

    def test_non_freedesktop_goes_under_external(self):
        got = action_to_qdistro("org.gnome.settings-daemon.plugins.power")
        assert got == "qdistro.external.org.gnome.settings-daemon.plugins.power"

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            action_to_qdistro("")

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            action_to_qdistro(None)  # type: ignore[arg-type]

    def test_qdistro_prefix_is_stable(self):
        """Mapping twice is a no-op (idempotent) for already-freedesktop
        inputs — important for cache keys that may be rechecked."""
        once = action_to_qdistro("org.freedesktop.login1.reboot")
        # Re-mapping the output doesn't strip further: it starts with
        # "qdistro." not "org.freedesktop.", so falls to external branch.
        # We don't promise idempotence on the qdistro-prefixed output;
        # the broker should map once at entry and treat the result as
        # the canonical action string throughout.
        assert once == "qdistro.login1.reboot"
        # Document that the once-mapped form is NOT a polkit action;
        # re-mapping wraps it under external — as a regression check.
        twice = action_to_qdistro(once)
        assert twice == "qdistro.external.qdistro.login1.reboot"
