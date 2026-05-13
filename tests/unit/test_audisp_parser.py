"""Tests for the audispd plugin's AVC line parser (spec/30 step 7).

The plugin runs as a long-lived child of audispd inside the VM; the
parser is isolated here so we can exercise it on the host with hand-
crafted fixtures pulled straight from /var/log/audit/audit.log."""
from __future__ import annotations

import pytest

from qdistro_audisp_parser import (
    parse_avc_line,
    is_qdistro_subj_type,
    avc_to_broker_args,
)


# --- canonical Tumbleweed AVC lines --------------------------------

DENIED_FILE_READ = (
    'type=AVC msg=audit(1614729383.123:456): avc:  denied  '
    '{ read open } for  pid=1234 comm="firefox" '
    'path="/etc/krb5.conf" dev="vda1" ino=12345 '
    'scontext=staff_u:staff_r:qdistro_tier1_t:s0 '
    'tcontext=system_u:object_r:krb5_conf_t:s0 '
    'tclass=file permissive=0'
)

DENIED_DIR_WRITE_PERMISSIVE = (
    'type=AVC msg=audit(1614729384.500:457): avc:  denied  '
    '{ write } for  pid=99 comm="sleep" '
    'scontext=staff_u:staff_r:qdistro_tier1_t:s0 '
    'tcontext=root:object_r:user_home_t:s0 '
    'tclass=dir permissive=1'
)

GRANTED_DIR_SEARCH = (
    'type=AVC msg=audit(1614729385.0:458): avc:  granted  '
    '{ search } for  pid=2 comm="x" '
    'scontext=staff_u:staff_r:qdistro_tier1_t:s0 '
    'tcontext=root:object_r:proc_t:s0 tclass=dir'
)

NON_QDISTRO_AVC = (
    'type=AVC msg=audit(1614729386.0:459): avc:  denied  '
    '{ read } for  pid=1 comm="systemd" '
    'scontext=system_u:system_r:init_t:s0 '
    'tcontext=system_u:object_r:foo_t:s0 '
    'tclass=file permissive=0'
)

NODE_PREFIXED = (
    'node=qdwin type=AVC msg=audit(1614729387.0:460): avc:  '
    'denied  { connect } for  pid=42 comm="qdwin-x" '
    'scontext=staff_u:staff_r:qdistro_tier1_t:s0 '
    'tcontext=staff_u:object_r:user_tmp_t:s0 '
    'tclass=unix_stream_socket permissive=0'
)


class TestParseAvcLine:
    def test_denied_file_read_extracts_all_fields(self):
        rec = parse_avc_line(DENIED_FILE_READ)
        assert rec is not None
        assert rec["verdict"] == "denied"
        assert rec["perms"] == "read open"
        assert rec["pid"] == 1234
        assert rec["comm"] == "firefox"
        assert rec["path"] == "/etc/krb5.conf"
        assert rec["scontext"] == "staff_u:staff_r:qdistro_tier1_t:s0"
        assert rec["tcontext"] == "system_u:object_r:krb5_conf_t:s0"
        assert rec["tclass"] == "file"
        assert rec["permissive"] == 0
        assert rec["subj_type"] == "qdistro_tier1_t"
        assert rec["serial"] == 456
        assert rec["ts"] == pytest.approx(1614729383.123)

    def test_permissive_one_extracted_as_int(self):
        rec = parse_avc_line(DENIED_DIR_WRITE_PERMISSIVE)
        assert rec is not None
        assert rec["permissive"] == 1
        assert rec["verdict"] == "denied"
        assert rec["perms"] == "write"

    def test_granted_verdict_recognised(self):
        rec = parse_avc_line(GRANTED_DIR_SEARCH)
        assert rec is not None
        assert rec["verdict"] == "granted"
        # granted records often omit permissive — default to 0
        assert rec["permissive"] == 0

    def test_node_prefix_does_not_block(self):
        # audispd's `node=` field can prepend the line on multi-host
        # setups; the envelope regex must tolerate that.
        rec = parse_avc_line(NODE_PREFIXED)
        assert rec is not None
        assert rec["tclass"] == "unix_stream_socket"
        assert rec["subj_type"] == "qdistro_tier1_t"

    def test_non_avc_line_returns_none(self):
        assert parse_avc_line(
            "type=SYSCALL msg=audit(1.0:1): a0=1 a1=2") is None
        assert parse_avc_line("") is None
        assert parse_avc_line("garbage line\n") is None

    def test_missing_pid_defaults_to_zero(self):
        # Some early-boot AVC records have no pid field. Don't crash;
        # default to 0 so the broker side can store the row.
        line = (
            'type=AVC msg=audit(1.0:2): avc:  denied  { read } for  '
            'comm="early" scontext=staff_u:staff_r:qdistro_tier1_t:s0 '
            'tcontext=root:object_r:foo_t:s0 tclass=file permissive=0'
        )
        rec = parse_avc_line(line)
        assert rec is not None
        assert rec["pid"] == 0


class TestSubjectTypeFilter:
    def test_qdistro_tier1_recognised(self):
        assert is_qdistro_subj_type("qdistro_tier1_t")

    def test_other_qdistro_prefix_recognised(self):
        # forward compat for hypothetical qdistro_tier2_t etc.
        assert is_qdistro_subj_type("qdistro_tier2_t")

    def test_empty_rejected(self):
        assert not is_qdistro_subj_type("")

    def test_unrelated_rejected(self):
        assert not is_qdistro_subj_type("init_t")
        assert not is_qdistro_subj_type("user_t")
        assert not is_qdistro_subj_type("qdistroFakey")


class TestAvcToBrokerArgs:
    def test_complete_record_round_trips(self):
        rec = parse_avc_line(DENIED_FILE_READ)
        args = avc_to_broker_args(rec)
        assert args["scontext"] == "staff_u:staff_r:qdistro_tier1_t:s0"
        assert args["tclass"] == "file"
        assert args["perms"] == "read open"
        assert args["pid"] == 1234
        assert args["comm"] == "firefox"
        assert args["path"] == "/etc/krb5.conf"
        assert args["permissive"] == 0
        assert args["verdict"] == "denied"
        assert args["ts"] == pytest.approx(1614729383.123)

    def test_missing_optional_fields_default_to_empty(self):
        # GRANTED_DIR_SEARCH has no path, no exe.
        rec = parse_avc_line(GRANTED_DIR_SEARCH)
        args = avc_to_broker_args(rec)
        assert args["path"] == ""
        assert args["exe"] == ""
        assert args["permissive"] == 0
