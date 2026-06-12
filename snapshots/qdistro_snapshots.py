"""qdistro Snapper D-Bus client wrapper — task(115) / spec/19 Phase-8.

Per doc/filesystem.md §"D-Bus API reference". The
broker calls Snapper directly on the system bus rather than
shelling out — saves ~140 ms per snapshot (10 ms D-Bus vs ~150 ms
shell-out) and avoids the polkit prompt the snapper CLI triggers
under non-root callers.

This is a pure-python wrapper that takes an injectable transport
callable; tests stub the transport so we never touch dbus-python
or a real Snapper. The real broker plumbs in a `dbus.SystemBus()`
binding at startup; the transport contract is one function with
the signature::

    transport(method: str, *args) -> Any

It must call ``org.opensuse.Snapper`` on
``/org/opensuse/Snapper`` and return the method result. Errors
are raised as exceptions (any subclass — the wrapper catches
them and rewraps).

Known method shapes (verified against
snapper/doc/dbus-protocol.txt):

    CreateSingleSnapshot(config, description, cleanup, userdata) -> i (number)
    CreatePreSnapshot(config, description, cleanup, userdata) -> i
    CreatePostSnapshot(config, pre-number, description, cleanup, userdata) -> i
    ListSnapshots(config) -> array of (num, type, pre-num, date, uid, desc, cleanup, userdata)
    GetFiles(config, num1, num2) -> array of (filename, status)
    DeleteSnapshots(config, [numbers]) -> nothing

Spec/19 explicitly notes there is NO ``GetDiff`` method — diff is
``GetFiles`` (per-file status) and ``CreateComparison`` (cached).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

# Userdata key the broker sets on every qdistro-originated snapshot
# so admin-app's Snapshots tab can highlight them vs. Snapper's
# own zypper-pre/post entries.
QDISTRO_USERDATA_KEY = "qdistro.origin"


class SnapshotError(Exception):
    """Generic snapshot operation failure. The wrapped exception
    is in .__cause__; .detail carries a short string for the
    audit row."""

    def __init__(self, msg: str, detail: str = ""):
        super().__init__(msg)
        self.detail = detail


def _norm_userdata(extras: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {QDISTRO_USERDATA_KEY: "1"}
    if extras:
        for k, v in extras.items():
            if not k or not isinstance(k, str):
                continue
            out[k] = "" if v is None else str(v)
    return out


class SnapperClient:
    """Thin façade around the org.opensuse.Snapper D-Bus surface.

    Instantiate with a transport callable; tests inject fakes,
    production injects ``dbus.Interface(...).get_dbus_method``.
    """

    def __init__(self, transport: Callable[..., Any]):
        self._transport = transport

    # ---- snapshot create ----------------------------------

    def create_single(self, config: str, description: str,
                      *, cleanup: str = "number",
                      userdata: dict[str, str] | None = None) -> int:
        try:
            n = self._transport(
                "CreateSingleSnapshot", config, description,
                cleanup, _norm_userdata(userdata))
        except Exception as e:
            raise SnapshotError(
                f"CreateSingleSnapshot({config!r}) failed",
                detail=str(e)[:200]) from e
        return int(n)

    def create_pre(self, config: str, description: str,
                   *, cleanup: str = "number",
                   userdata: dict[str, str] | None = None) -> int:
        try:
            n = self._transport(
                "CreatePreSnapshot", config, description,
                cleanup, _norm_userdata(userdata))
        except Exception as e:
            raise SnapshotError(
                f"CreatePreSnapshot({config!r}) failed",
                detail=str(e)[:200]) from e
        return int(n)

    def create_post(self, config: str, pre_number: int,
                    description: str,
                    *, cleanup: str = "number",
                    userdata: dict[str, str] | None = None) -> int:
        try:
            n = self._transport(
                "CreatePostSnapshot", config, int(pre_number),
                description, cleanup, _norm_userdata(userdata))
        except Exception as e:
            raise SnapshotError(
                f"CreatePostSnapshot({config!r}, "
                f"pre={pre_number}) failed",
                detail=str(e)[:200]) from e
        return int(n)

    # ---- list / inspect / delete ---------------------------

    def list(self, config: str) -> list[dict]:
        """Return ListSnapshots normalised to dicts.

        Snapper returns an array of tuples; we flatten so callers
        don't have to remember the order. ts is float (seconds
        since epoch) for downstream JSON serialisation.
        """
        try:
            raw = self._transport("ListSnapshots", config)
        except Exception as e:
            raise SnapshotError(
                f"ListSnapshots({config!r}) failed",
                detail=str(e)[:200]) from e
        out = []
        for row in raw:
            num, type_, pre_num, date, uid, desc, cleanup, ud = row
            out.append({
                "num": int(num),
                "type": str(type_),
                "pre_num": int(pre_num),
                "ts": float(date),
                "uid": int(uid),
                "description": str(desc),
                "cleanup": str(cleanup),
                "userdata": dict(ud) if ud else {},
                "qdistro_origin":
                    bool((ud or {}).get(QDISTRO_USERDATA_KEY)),
            })
        return out

    def get_files(self, config: str,
                  num1: int, num2: int) -> list[dict]:
        """GetFiles — the diff API. Spec/19 explicitly notes
        Snapper has NO GetDiff method; this is the shape callers
        want."""
        try:
            raw = self._transport("GetFiles", config,
                                  int(num1), int(num2))
        except Exception as e:
            raise SnapshotError(
                f"GetFiles({config!r}, {num1}, {num2}) failed",
                detail=str(e)[:200]) from e
        out = []
        for filename, status in raw:
            out.append({"path": str(filename),
                        "status": str(status)})
        return out

    def delete_snapshots(self, config: str,
                         numbers: Iterable[int]) -> None:
        nums = [int(n) for n in numbers]
        try:
            self._transport("DeleteSnapshots", config, nums)
        except Exception as e:
            raise SnapshotError(
                f"DeleteSnapshots({config!r}, {nums}) failed",
                detail=str(e)[:200]) from e


# ---- "before/after" helpers (broker.SnapshotBefore wiring) -----

def snapshot_before(client: SnapperClient, config: str,
                    description: str,
                    *, caller_uid: int | None = None,
                    caller_exe: str | None = None) -> int:
    """SDK helper: take a single snapshot tagged with the caller's
    identity for forensic correlation in the audit log.

    Returns the snapshot number. Idempotent in the sense that
    repeated calls just stack snapshots — Snapper's NUMBER_LIMIT
    cleanup reaps the oldest.
    """
    ud: dict[str, str] = {"qdistro.action": "before"}
    if caller_uid is not None:
        ud["qdistro.caller_uid"] = str(int(caller_uid))
    if caller_exe:
        ud["qdistro.caller_exe"] = str(caller_exe)
    return client.create_single(config, description, userdata=ud)


def vault_snapshot(client: SnapperClient,
                   item_action: str,
                   item_name: str,
                   config: str = "qdistro_vaults") -> int:
    """Pwd vault per-mutation snapshot. Spec/19 §"Caveats" calls
    out the per-mutation cadence on the vault subvolume. Caller
    is the broker, on every successful add/edit/delete.
    """
    desc = f"vault {item_action} {item_name}"
    return client.create_single(
        config, desc, userdata={
            "qdistro.action": item_action,
            "qdistro.item": item_name,
            "qdistro.ts": str(int(time.time())),
        })


# ---- backup-recipient parsing (rage-encryption pipeline) -------

def parse_backup_recipients(text: str) -> list[str]:
    """Read /etc/qdistro/backup-recipients.txt — one age recipient
    per line. ``#`` comments and blank lines skipped. Returns the
    list in source order; deduplicated while preserving order.
    """
    seen: set[str] = set()
    out: list[str] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if not ln.startswith("age1"):
            # rage-encryption recipients always start with age1.
            # Skip malformed lines silently — admin can lint via
            # the admin-app Snapshots tab.
            continue
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
    return out


def render_backup_command(
        snap_path: str,
        parent_snap_path: str | None,
        recipients_file: str,
        ssh_target: str,
        remote_path: str,
) -> list[str]:
    """Build the canonical encrypted-export command list per
    spec/19 §"Encryption pipeline". Returns a list suitable for
    `subprocess.run(..., check=True)` with `shell=True`.

    Output is a single shell pipeline string wrapped as
    ``["bash", "-c", "..."]``. The remote_path is shell-quoted;
    snap paths are passed through `repr()`-style quoting which
    rejects shell metacharacters (the broker validates upstream).
    """
    if not snap_path or " " in snap_path or ";" in snap_path:
        raise ValueError("invalid snap_path")
    if parent_snap_path and (" " in parent_snap_path
                              or ";" in parent_snap_path):
        raise ValueError("invalid parent_snap_path")
    if " " in recipients_file or ";" in recipients_file:
        raise ValueError("invalid recipients_file")
    parent = (f"-p {parent_snap_path}" if parent_snap_path else "")
    pipeline = (
        f"btrfs send {parent} {snap_path} "
        f"| rage -e -R {recipients_file} "
        f"| ssh {ssh_target} 'cat > {remote_path}'"
    )
    return ["bash", "-c", pipeline]
