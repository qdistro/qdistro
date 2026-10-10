"""Host tests for tier5-vm/vmapps-scan.py.

The scanner's guest-access layer has a --root mode that reads desktop
files and binaries from a host directory, so every selection rule the
vmapps-dynamic plan pins — precedence, masking, field codes, aliasing,
TryExec, denylist, foot-required, atomic publish, freshness — is
exercised here end-to-end through the CLI with tmp_path fixtures.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "tier5-vm" / "vmapps-scan.py"

LOCAL_APPS = "usr/local/share/applications"
SHARE_APPS = "usr/share/applications"


def _desktop(
    name: str,
    exec_: str | None = None,
    *,
    type_: str = "Application",
    icon: str = "",
    comment: str = "",
    hidden: bool = False,
    nodisplay: bool = False,
    terminal: bool = False,
    onlyshowin: str = "",
    notshowin: str = "",
    tryexec: str = "",
    dbus_activatable: bool = False,
) -> str:
    lines = ["[Desktop Entry]", f"Type={type_}", f"Name={name}"]
    if exec_ is not None:
        lines.append(f"Exec={exec_}")
    if icon:
        lines.append(f"Icon={icon}")
    if comment:
        lines.append(f"Comment={comment}")
    if hidden:
        lines.append("Hidden=true")
    if nodisplay:
        lines.append("NoDisplay=true")
    if terminal:
        lines.append("Terminal=true")
    if onlyshowin:
        lines.append(f"OnlyShowIn={onlyshowin}")
    if notshowin:
        lines.append(f"NotShowIn={notshowin}")
    if tryexec:
        lines.append(f"TryExec={tryexec}")
    if dbus_activatable:
        lines.append("DBusActivatable=true")
    return "\n".join(lines) + "\n"


def _write(root: Path, rel: str, content: str | bytes, mode: int = 0o644) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content)
    p.chmod(mode)
    return p


def _exe(root: Path, rel: str) -> Path:
    return _write(root, rel, "#!/bin/sh\n", 0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (guest root, fake base file, output manifest path)."""
    root = tmp_path / "guest"
    root.mkdir()
    base = tmp_path / "base.qcow2"
    base.write_bytes(b"qcow2-placeholder")
    out = tmp_path / "out" / "base.json"
    return root, base, out


def _scan(root: Path, base: Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCANNER),
         "--root", str(root), "--base", str(base), "--out", str(out),
         *extra],
        text=True, capture_output=True, check=False,
    )


def _manifest(out: Path) -> dict:
    return json.loads(out.read_text())


def _apps(out: Path) -> dict[str, dict]:
    return {row["appId"]: row for row in _manifest(out)["apps"]}


def _argv(row: dict) -> list[str]:
    return json.loads(row["execArgv"])


# ---------------------------------------------------------------------------
# Basic selection
# ---------------------------------------------------------------------------

def test_fixture_dirs_produce_manifest(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/org.kde.konsole.desktop",
           _desktop("Konsole", "konsole", icon="utilities-terminal",
                    comment="KDE terminal"))
    _write(root, f"{SHARE_APPS}/firefox.desktop",
           _desktop("Firefox", "firefox"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert set(apps) == {"tier5/konsole", "tier5/firefox"}
    konsole = apps["tier5/konsole"]
    assert konsole["desktopId"] == "org.kde.konsole"
    assert konsole["name"] == "Konsole"
    assert konsole["iconName"] == "utilities-terminal"
    assert konsole["comment"] == "KDE terminal"
    assert _argv(konsole) == ["konsole"]


def test_identity_tuple_records_the_base_file(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    st = base.stat()
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    doc = _manifest(out)
    assert doc["schema"] == 1
    assert doc["base"] == str(base)
    assert doc["baseIno"] == st.st_ino
    assert doc["baseSize"] == st.st_size
    assert doc["baseMtime"] == int(st.st_mtime)


def test_manifest_is_0644_and_published_atomically(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert (out.stat().st_mode & 0o777) == 0o644
    leftovers = list(out.parent.glob(".base.json.*"))
    assert leftovers == []


def test_empty_catalogue_publishes_empty_manifest(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert _manifest(out)["apps"] == []


# ---------------------------------------------------------------------------
# Precedence and masking
# ---------------------------------------------------------------------------

def test_usr_local_wins_over_usr_share(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/org.kde.konsole.desktop",
           _desktop("Konsole", "konsole"))
    _write(root, f"{LOCAL_APPS}/org.kde.konsole.desktop",
           _desktop("Konsole LOCAL", "konsole -e hi"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert apps["tier5/konsole"]["name"] == "Konsole LOCAL"
    assert _argv(apps["tier5/konsole"]) == ["konsole", "-e", "hi"]


def test_hidden_in_usr_local_masks_usr_share(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _write(root, f"{SHARE_APPS}/firefox.desktop", _desktop("Firefox", "firefox"))
    _write(root, f"{LOCAL_APPS}/firefox.desktop", _desktop("Firefox", "firefox",
                                                          hidden=True))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert _apps(out) == {}


def test_malformed_usr_local_does_not_mask_usr_share(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/firefox.desktop", _desktop("Firefox", "firefox"))
    _write(root, f"{LOCAL_APPS}/firefox.desktop",
           b"[Desktop Entry\nno-equals-here")
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert "tier5/firefox" in _apps(out)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def test_nodisplay_terminal_onlyshowin_dropped_notshowin_kept(
    tmp_path: Path,
) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/a.desktop",
           _desktop("A", "a", nodisplay=True))
    _write(root, f"{SHARE_APPS}/b.desktop",
           _desktop("B", "b", terminal=True))
    _write(root, f"{SHARE_APPS}/c.desktop",
           _desktop("C", "c", onlyshowin="GNOME;"))
    _write(root, f"{SHARE_APPS}/d.desktop",
           _desktop("D", "d", notshowin="KDE;"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert set(_apps(out)) == {"tier5/d"}


def test_type_and_name_and_exec_required(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/link.desktop",
           _desktop("Link", "x", type_="Link"))
    _write(root, f"{SHARE_APPS}/noname.desktop", "[Desktop Entry]\n"
           "Type=Application\nExec=x\n")
    _write(root, f"{SHARE_APPS}/noexec.desktop",
           _desktop("NoExec", None, dbus_activatable=True))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert _apps(out) == {}


def test_dbus_activatable_with_exec_is_kept(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/org.kde.konsole.desktop",
           _desktop("Konsole", "konsole", dbus_activatable=True))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert _argv(_apps(out)["tier5/konsole"]) == ["konsole"]


# ---------------------------------------------------------------------------
# Field codes and quoting
# ---------------------------------------------------------------------------

def test_kate_dash_b_and_env_prefix_preserved(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/org.kde.kate.desktop",
           _desktop("Kate", "kate -b"))
    _write(root, f"{SHARE_APPS}/foo.desktop",
           _desktop("Foo", "env FOO=bar BAZ=qux foo --flag"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert _argv(apps["tier5/kate"]) == ["kate", "-b"]
    assert _argv(apps["tier5/foo"]) == ["env", "FOO=bar", "BAZ=qux",
                                        "foo", "--flag"]


def test_quoted_argv_and_field_codes(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/app.desktop",
           _desktop("Quoted App", 'app --title "hello world" --x=%c %u %%',
                    icon="appicon"))
    _write(root, f"{SHARE_APPS}/app2.desktop",
           _desktop("IconApp", "app2 %i %k %f", icon="appicon"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert _argv(apps["tier5/app"]) == [
        "app", "--title", "hello world", "--x=Quoted App", "%",
    ]
    # %i expands to two args, %k to the .desktop path, %f drops.
    assert _argv(apps["tier5/app2"]) == [
        "app2", "--icon", "appicon",
        f"/{SHARE_APPS}/app2.desktop",
    ]


def test_unknown_field_code_drops_the_row(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/bad.desktop",
           _desktop("Bad", "bad %v"))
    _write(root, f"{SHARE_APPS}/good.desktop",
           _desktop("Good", "good"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert set(_apps(out)) == {"tier5/good"}
    assert "unsupported field code" in res.stderr


# ---------------------------------------------------------------------------
# TryExec
# ---------------------------------------------------------------------------

def test_tryexec_missing_or_nonexec_drops_row(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/gone.desktop",
           _desktop("Gone", "gone", tryexec="definitely-missing"))
    _write(root, f"{SHARE_APPS}/noexec.desktop",
           _desktop("NoExec", "noexec", tryexec="noexec"))
    _write(root, "usr/bin/noexec", "data\n", 0o644)
    _write(root, f"{SHARE_APPS}/ok.desktop",
           _desktop("Ok", "ok", tryexec="ok"))
    _exe(root, "usr/bin/ok")
    _write(root, f"{SHARE_APPS}/abs.desktop",
           _desktop("Abs", "abs", tryexec="/usr/bin/ok"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert set(_apps(out)) == {"tier5/ok", "tier5/abs"}


# ---------------------------------------------------------------------------
# Denylist and aliasing
# ---------------------------------------------------------------------------

def test_footclient_and_footserver_denied(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/foot.desktop", _desktop("Foot", "foot"))
    _write(root, f"{SHARE_APPS}/footclient.desktop",
           _desktop("FootClient", "footclient"))
    _write(root, f"{SHARE_APPS}/foot-server.desktop",
           _desktop("FootServer", "foot-server"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert set(apps) == {"tier5/foot"}
    assert _argv(apps["tier5/foot"]) == ["foot"]


def test_dropin_denylist_file(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/firefox.desktop", _desktop("Firefox", "firefox"))
    _write(root, f"{SHARE_APPS}/keep.desktop", _desktop("Keep", "keep"))
    deny = tmp_path / "denylist"
    deny.write_text("firefox\n# comment\n\n")
    res = _scan(root, base, out, "--denylist", str(deny))
    assert res.returncode == 0, res.stderr
    assert set(_apps(out)) == {"tier5/keep"}


def test_unmapped_desktop_id_becomes_tier5_id(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/org.example.Fancy.desktop",
           _desktop("Fancy", "fancy"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert set(apps) == {"tier5/org.example.Fancy"}
    assert apps["tier5/org.example.Fancy"]["desktopId"] == "org.example.Fancy"


def test_alias_collision_mapped_row_wins(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    # org.kde.konsole maps to tier5/konsole; a second file whose unmapped
    # id would also be tier5/konsole is dropped.
    _write(root, f"{SHARE_APPS}/org.kde.konsole.desktop",
           _desktop("Konsole", "konsole"))
    _write(root, f"{SHARE_APPS}/konsole.desktop",
           _desktop("Impostor", "konsole2"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert set(apps) == {"tier5/konsole"}
    assert apps["tier5/konsole"]["name"] == "Konsole"


def test_full_legacy_alias_map(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    expected = {
        "org.kde.kate": "tier5/kate",
        "org.kde.dolphin": "tier5/dolphin",
        "org.kde.kcalc": "tier5/kcalc",
        "org.gnome.baobab": "tier5/baobab",
        "org.gnome.TextEditor": "tier5/gnome-text-editor",
        "org.gnome.Nautilus": "tier5/nautilus",
        "org.gnome.Calculator": "tier5/gnome-calculator",
    }
    for desktop_id in expected:
        _write(root, f"{SHARE_APPS}/{desktop_id}.desktop",
               _desktop(desktop_id, "x"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    apps = _apps(out)
    assert set(apps) == set(expected.values())
    for desktop_id, app_id in expected.items():
        assert apps[app_id]["desktopId"] == desktop_id


# ---------------------------------------------------------------------------
# Foot-required
# ---------------------------------------------------------------------------

def test_catalogue_without_guest_foot_fails(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _write(root, f"{SHARE_APPS}/firefox.desktop", _desktop("Firefox", "firefox"))
    res = _scan(root, base, out)
    assert res.returncode == 1
    assert not out.exists()
    assert "foot" in res.stderr


def test_foot_in_usr_local_satisfies_requirement(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/local/bin/foot")
    _write(root, f"{SHARE_APPS}/firefox.desktop", _desktop("Firefox", "firefox"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert "tier5/firefox" in _apps(out)


# ---------------------------------------------------------------------------
# Malformed inputs
# ---------------------------------------------------------------------------

def test_malformed_entries_skipped_not_fatal(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/binary.desktop", b"\xff\xfe\x00bad")
    _write(root, f"{SHARE_APPS}/nosection.desktop", "[Other]\nKey=v\n")
    _write(root, f"{SHARE_APPS}/ok.desktop", _desktop("Ok", "ok"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    assert set(_apps(out)) == {"tier5/ok"}


# ---------------------------------------------------------------------------
# --check (reconcile fast path) and --emit-stale
# ---------------------------------------------------------------------------

def test_check_fresh_manifest_passes(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/ok.desktop", _desktop("Ok", "ok"))
    assert _scan(root, base, out).returncode == 0
    res = subprocess.run(
        [sys.executable, str(SCANNER), "--check", str(out),
         "--base", str(base)],
        text=True, capture_output=True, check=False,
    )
    assert res.returncode == 0, res.stderr


def test_check_stale_identity_fails(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _write(root, f"{SHARE_APPS}/ok.desktop", _desktop("Ok", "ok"))
    assert _scan(root, base, out).returncode == 0
    # Replace the base — different inode and mtime.
    base.unlink()
    base.write_bytes(b"new-image")
    os.utime(base, (base.stat().st_atime, base.stat().st_mtime + 5))
    res = subprocess.run(
        [sys.executable, str(SCANNER), "--check", str(out),
         "--base", str(base)],
        text=True, capture_output=True, check=False,
    )
    assert res.returncode == 1
    assert "identity mismatch" in res.stderr


def test_check_rejects_empty_and_stale_manifests(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    assert _scan(root, base, out).returncode == 0  # empty catalogue
    res = subprocess.run(
        [sys.executable, str(SCANNER), "--check", str(out),
         "--base", str(base)],
        text=True, capture_output=True, check=False,
    )
    assert res.returncode == 1
    out.write_text(json.dumps({"schema": 1, "stale": "x", "apps": []}))
    res = subprocess.run(
        [sys.executable, str(SCANNER), "--check", str(out),
         "--base", str(base)],
        text=True, capture_output=True, check=False,
    )
    assert res.returncode == 1
    out.write_text("{not json")
    res = subprocess.run(
        [sys.executable, str(SCANNER), "--check", str(out),
         "--base", str(base)],
        text=True, capture_output=True, check=False,
    )
    assert res.returncode == 1


def test_emit_stale_writes_marker_atomically(tmp_path: Path) -> None:
    out = tmp_path / "m" / "base.json"
    res = subprocess.run(
        [sys.executable, str(SCANNER), "--emit-stale", str(out),
         "--reason", "base replaced during scan"],
        text=True, capture_output=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(out.read_text())
    assert doc == {"schema": 1, "stale": "base replaced during scan",
                   "apps": []}
    assert (out.stat().st_mode & 0o777) == 0o644


def test_duplicate_appid_never_emitted(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    # Two unmapped ids colliding is impossible (appId derives from the
    # id), so the only collision surface is mapped-vs-unmapped — covered
    # above. Pin the invariant directly at the manifest level: appIds
    # are unique.
    _write(root, f"{SHARE_APPS}/a.desktop", _desktop("A", "a"))
    _write(root, f"{SHARE_APPS}/b.desktop", _desktop("B", "b"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    ids = [r["appId"] for r in _manifest(out)["apps"]]
    assert len(ids) == len(set(ids))


def test_appid_outside_policy_key_charset_is_dropped(tmp_path: Path) -> None:
    root, base, out = _fixture(tmp_path)
    _exe(root, "usr/bin/foot")
    _exe(root, "usr/bin/eviltool")
    # The appId is the wrapper's --policy-key and the domain's bound
    # identity; a desktop filename that derives an out-of-charset appId
    # (space, +, UTF-8) must never be published — the wrapper would
    # reject it at launch anyway, and silently re-binding could alias
    # another app's guest (sol r4).
    _write(root, f"{SHARE_APPS}/foot.desktop", _desktop("Foot", "foot"))
    _write(root, f"{SHARE_APPS}/my app.desktop", _desktop("Bad", "eviltool"))
    _write(root, f"{SHARE_APPS}/evil+tool.desktop", _desktop("Bad2", "eviltool"))
    res = _scan(root, base, out)
    assert res.returncode == 0, res.stderr
    ids = [r["appId"] for r in _manifest(out)["apps"]]
    assert ids == ["tier5/foot"], ids
    assert "outside the policy-key charset" in res.stderr
