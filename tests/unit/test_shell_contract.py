"""Guard: the qdshell contract fixture matches authoritative qdistro constants.

`tests/contracts/qdistro_shell_contract.json` is the single source of truth
for the small set of identifiers that qdshell hand-mirrors in QML/JS (tier
secctx prefixes, the AdminBroker1/SessionManager1 D-Bus names + object paths,
and the broker reply fields qdshell parses). The qdshell side has its own
guard that asserts its literals match the fixture; THIS test asserts the
fixture has not drifted away from the real qdistro Python producers.

Anchoring strategy (see the codex review that gated this work):
  - tier4 / disp prefixes: compared against the live module constants
    (cheap, side-effect-free imports).
  - broker / session-manager bus names + object paths: read as named-constant
    assignments straight from the daemon source (no heavy import).
  - tier3 / tier5 prefixes: no Python runtime constant exists (qdwin emits
    those secctx app_ids in C); the fixture is their anchor, so we only assert
    their shape here.

Deliberately narrow. Do not grow this into a schema/codegen.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

# qdistro/tests/unit/test_shell_contract.py -> qdistro repo root == parents[2]
QDISTRO = Path(__file__).resolve().parents[2]
CONTRACT_PATH = QDISTRO / "tests" / "contracts" / "qdistro_shell_contract.json"


def _strip_doc(obj):
    """Drop the human-facing _README/_doc keys so they never gate the data."""
    if isinstance(obj, dict):
        return {k: _strip_doc(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, list):
        return [_strip_doc(v) for v in obj]
    return obj


@pytest.fixture(scope="module")
def contract():
    with CONTRACT_PATH.open(encoding="utf-8") as f:
        return _strip_doc(json.load(f))


def _read_const(rel_source: str, name: str) -> str:
    """Read a top-level ``NAME = "value"`` string assignment from a source file."""
    text = (QDISTRO / rel_source).read_text(encoding="utf-8")
    m = re.search(rf'^{re.escape(name)}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    assert m, f"{name} not found as a string constant in {rel_source}"
    return m.group(1)


def _find_function(rel_source: str, func_name: str) -> ast.FunctionDef:
    src = (QDISTRO / rel_source).read_text(encoding="utf-8")
    matches = [n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == func_name]
    assert matches, f"{func_name} not found in {rel_source}"
    # Anchor must be unambiguous — if a second same-named def appears, this guard
    # could silently point at the wrong producer.
    assert len(matches) == 1, (
        f"{func_name} is ambiguous in {rel_source} ({len(matches)} defs); "
        "narrow the anchor before trusting this guard.")
    return matches[0]


def _produced_dict_keys(rel_source: str, func_name: str) -> set[str]:
    """String keys of the dict(s) a function RETURNS or `.append()`s.

    Proves the PRODUCER still emits the fields the contract promises. Narrowed to
    returned / appended dicts (not every dict literal in the function) so an
    unrelated nested dict can't mask a dropped reply field.
    """
    fn = _find_function(rel_source, func_name)
    dicts: list[ast.Dict] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            dicts.append(node.value)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"):
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    dicts.append(arg)
    assert dicts, f"no returned/appended dict found in {func_name} ({rel_source})"
    keys: set[str] = set()
    for d in dicts:
        for k in d.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
    return keys


def _appended_tuple_arg_names(rel_source: str, func_name: str) -> list[str]:
    """Inner argument names of the single ``x.append((...))`` tuple in a function.

    The broker builds ListReceivers rows as
    ``out.append((dbus.Int32(uid), dbus.String(svc), dbus.String(friendly)))`` —
    this returns ['uid', 'svc', 'friendly'] so a POSITIONAL swap (which keeps the
    a(iss) signature, and which qdshell reads positionally as r[0]/r[1]/r[2]) is
    caught.
    """
    fn = _find_function(rel_source, func_name)
    tuples: list[ast.Tuple] = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args
                and isinstance(node.args[0], ast.Tuple)):
            tuples.append(node.args[0])
    assert len(tuples) == 1, (
        f"expected exactly one append((tuple)) in {func_name}, found {len(tuples)}")
    names: list[str] = []
    for el in tuples[0].elts:
        # each element is like dbus.Int32(uid) / dbus.String(svc)
        if isinstance(el, ast.Call) and el.args and isinstance(el.args[0], ast.Name):
            names.append(el.args[0].id)
        elif isinstance(el, ast.Name):
            names.append(el.id)
        else:
            names.append("?")
    return names


def _method_out_signature(rel_source: str, func_name: str) -> str:
    """The ``out_signature=`` kwarg on a @dbus.service.method-decorated method."""
    fn = _find_function(rel_source, func_name)
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "out_signature" and isinstance(kw.value, ast.Constant):
                    return kw.value.value
    raise AssertionError(f"no out_signature on {func_name} in {rel_source}")


def test_contract_fixture_is_present_and_well_formed(contract):
    assert set(contract) >= {"secctx_prefixes", "dbus", "broker_reply_fields", "env"}


def test_secctx_prefix_shapes(contract):
    prefixes = contract["secctx_prefixes"]
    assert set(prefixes) == {"tier3", "tier4", "tier5", "disp"}
    for key, value in prefixes.items():
        assert value.startswith("qdistro."), value
        assert value.endswith("."), f"{key} prefix must end with '.': {value!r}"


def test_tier4_prefix_matches_runtime_constant(contract):
    import sys
    sys.path.insert(0, str(QDISTRO / "tier4-vm"))
    import tier4_chrome
    assert contract["secctx_prefixes"]["tier4"] == tier4_chrome.TIER4_SECCTX_PREFIX


def test_disp_prefix_matches_runtime_constant(contract):
    import sys
    sys.path.insert(0, str(QDISTRO / "session_manager"))
    import qdistro_disposables
    assert contract["secctx_prefixes"]["disp"] == qdistro_disposables.SECCTX_APPID_PREFIX


def test_admin_broker_dbus_identifiers(contract):
    admin = contract["dbus"]["admin_broker"]
    # The broker declares its own bus name; the session manager declares the
    # broker's name + path it dials. Both must agree with the fixture.
    assert admin["bus_name"] == _read_const("broker/qdistro_admin_broker.py", "BUS_NAME")
    # The broker's OWN exported object path (the real producer), not just the
    # session manager's dialed copy — otherwise the broker could move its path
    # while this guard stayed green.
    assert admin["object_path"] == _read_const("broker/qdistro_admin_broker.py", "OBJ_PATH")
    # And the session manager's copies of both must agree with the producer.
    assert admin["bus_name"] == _read_const(
        "session_manager/qdistro_session_manager.py", "ADMIN_BROKER_BUS_NAME")
    assert admin["object_path"] == _read_const(
        "session_manager/qdistro_session_manager.py", "ADMIN_BROKER_OBJ_PATH")
    # interface name == bus name for these well-known services
    assert admin["interface"] == admin["bus_name"]


def test_session_manager_dbus_identifiers(contract):
    sm = contract["dbus"]["session_manager"]
    assert sm["bus_name"] == _read_const(
        "session_manager/qdistro_session_manager.py", "BUS_NAME")
    assert sm["object_path"] == _read_const(
        "session_manager/qdistro_session_manager.py", "OBJ_PATH")
    assert sm["interface"] == sm["bus_name"]


# ── producer-to-fixture: the broker/session-manager must still EMIT every field
#    the contract promises qdshell. (The fixture-to-qdshell half lives in the
#    qdshell Node guard.) The producer may emit MORE fields than qdshell parses
#    (e.g. the argv_* rule selectors qdshell ignores), so these are subset checks.

def test_listrules_producer_still_emits_contract_fields(contract):
    produced = _produced_dict_keys("broker/qdistro_admin_broker.py", "ListRules")
    missing = set(contract["broker_reply_fields"]["ListRules"]) - produced
    assert not missing, (
        f"ListRules no longer emits contract field(s) {sorted(missing)} — "
        "the broker producer drifted from qdshell's parser.")


def test_listreceivers_producer_signature_matches(contract):
    spec = contract["broker_reply_fields"]["ListReceivers"]
    out_sig = _method_out_signature("broker/qdistro_admin_broker.py", "ListReceivers")
    # contract carries the element signature "(iss)"; the method returns an array.
    assert out_sig == "a" + spec["signature"], (
        f"ListReceivers out_signature {out_sig!r} != a{spec['signature']!r}")


def test_listreceivers_producer_tuple_order(contract):
    # Same a(iss) signature can hide a positional swap; pin the producer order.
    # contract tuple roles -> producer local var names that carry them:
    spec = contract["broker_reply_fields"]["ListReceivers"]
    names = _appended_tuple_arg_names("broker/qdistro_admin_broker.py", "ListReceivers")
    assert names == ["uid", "svc", "friendly"], (
        f"ListReceivers tuple order changed to {names}; qdshell reads it "
        "positionally (r[0]=uid, r[1]=service, r[2]=friendly_name).")
    assert len(names) == len(spec["tuple"])


def test_listsilos_producer_still_emits_contract_fields(contract):
    # ListSilos serializes Silo.to_dict(); assert that producer still has the
    # fields qdshell reads (uid, name).
    produced = _produced_dict_keys(
        "session_manager/qdistro_session_manager.py", "to_dict")
    missing = set(contract["broker_reply_fields"]["ListSilos"]["fields"]) - produced
    assert not missing, (
        f"Silo.to_dict no longer emits contract field(s) {sorted(missing)}")
