"""The complete owned firewall policy is reconciled in a single transaction."""
from types import SimpleNamespace

import pytest
import qdistro_session_manager as M


def test_reconcile_is_atomic_and_preserves_dynamic_sets(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(M.subprocess, 'run', run)
    ops = object.__new__(M._SystemOps)
    ops._nft_ensure_table()
    assert len(calls) == 1, calls
    argv, kwargs = calls[0]
    assert argv == ['nft', '-f', '-']
    batch = kwargs['input']
    assert 'flush table' not in batch and 'delete table' not in batch
    assert 'flush set' not in batch and 'delete set' not in batch
    assert 'meta skuid @blocked_uids drop' in batch
    assert 'in ip saddr @nat_subnets drop' in batch
    assert 'forward ip daddr @nat_subnets drop' in batch
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10'):
        assert network in batch
    for chain in ('out', 'in', 'forward', 'post'):
        assert f'flush chain inet {ops._NFT_TABLE} {chain}' in batch
        assert f'delete chain inet {ops._NFT_TABLE} {chain}' in batch
    assert 'udp dport 53 accept' in batch and 'tcp dport 53 accept' in batch
    assert 'ip saddr @nat_subnets masquerade' in batch
    # Every ensure reconciles; an incomplete listing can never short-circuit it.
    ops._nft_ensure_table()
    assert calls[0] == calls[1]


def test_failed_batch_refuses_silo_admission(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1, stdout='', stderr='injected nft failure')
    monkeypatch.setattr(M.subprocess, 'run', run)
    ops = object.__new__(M._SystemOps)
    with pytest.raises(RuntimeError, match='injected nft failure'):
        ops.nft_skuid_drop(2000, True)
    assert len(calls) == 1
    assert calls[0][0] == ['nft', '-f', '-']
