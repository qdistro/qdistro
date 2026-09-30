"""Strict transfer wire validation and a payload-free broker receipt ledger."""
import json
import secrets
import threading
import time
from collections import OrderedDict

MAX_BYTES = 1024 * 1024
TERMINAL = frozenset({'applied', 'declined', 'failed', 'rejected'})
STATES = TERMINAL | {'staged', 'unknown'}


def load_object(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 8192:
        raise ValueError('transfer metadata exceeds budget')
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError('transfer metadata must be an object')
    return obj


def identity(value, *, empty=False):
    if (not isinstance(value, str) or len(value) > 128 or '\x00' in value
            or (not empty and not value)):
        raise ValueError('invalid transfer identity')
    value.encode("utf-8", errors="strict")
    return value


def capabilities(raw):
    obj = load_object(raw)
    if type(obj.get('version')) is not int or obj['version'] != 1:
        raise ValueError('unsupported capabilities version')
    identity(obj.get('instance_id'))
    kinds = obj.get('kinds')
    if (not isinstance(kinds, list) or not 0 < len(kinds) <= 32
            or any(not isinstance(k, str) or not k or len(k) > 128
                   or '\x00' in k or '*' in k for k in kinds)
            or type(obj.get('max_bytes')) is not int or not 0 < obj['max_bytes'] <= MAX_BYTES
            or obj.get('encoding') != 'utf-8'
            or type(obj.get('available')) is not bool
            or type(obj.get('confirmation_required')) is not bool
            or not isinstance(obj.get('reason', ''), str)):
        raise ValueError('malformed capabilities')
    for kind in kinds:
        identity(kind)
    return {k: obj[k] for k in ('version', 'instance_id', 'kinds', 'max_bytes', 'encoding',
                              'available', 'confirmation_required')} | {'reason': obj.get('reason', '')[:160]}


def payload(kind, value, caps):
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('payload must be NUL-free UTF-8')
    if len(value.encode('utf-8', errors='strict')) > MAX_BYTES:
        raise ValueError('payload exceeds transfer limit')
    if (not caps['available'] or kind not in caps['kinds']
            or len(value.encode('utf-8')) > caps['max_bytes']):
        raise ValueError('receiver does not accept this transfer')


def receipt(instance='', token='', state='unknown', reason='outcome unavailable'):
    return {'version': 1, 'instance_id': instance, 'transfer_id': token,
            'state': state, 'reason': reason[:160]}


def validate_receipt(raw, expected_instance, expected_id=None):
    obj = load_object(raw)
    if (type(obj.get('version')) is not int or obj['version'] != 1
            or not isinstance(obj.get('state'), str) or obj['state'] not in STATES
            or obj.get('instance_id') != expected_instance
            or not isinstance(obj.get('reason'), str) or len(obj['reason']) > 160):
        raise ValueError('malformed receipt')
    token = identity(obj.get('transfer_id'), empty=obj['state'] in {'rejected','unknown'})
    if expected_id is not None and token != expected_id:
        raise ValueError('receipt identity changed')
    return receipt(expected_instance, token, obj['state'], obj['reason'])


class ReceiptLedger:
    """Actor-bound routing expires after TTL; capacity eviction only drops terminal receipts."""
    def __init__(self, clock=time.monotonic, capacity=256, ttl=600):
        self.clock, self.capacity, self.ttl = clock, capacity, ttl
        self.rows = OrderedDict()
        self.lock = threading.RLock()

    def _expire(self):
        now = self.clock()
        for token, row in list(self.rows.items()):
            if row['expires'] <= now:
                del self.rows[token]

    def reserve(self, actor, route, instance):
        with self.lock:
            self._expire()
            if len(self.rows) >= self.capacity:
                for token, row in list(self.rows.items()):
                    if row['receipt']['state'] in TERMINAL:
                        del self.rows[token]
                        break
            if len(self.rows) >= self.capacity:
                raise ValueError('receipt capacity exhausted')
            token = secrets.token_hex(32)
            row = {'actor': tuple(actor), 'route': route, 'expires': self.clock()+self.ttl,
                   'receiver_id': '', 'receipt': receipt(instance, token, 'staged', 'awaiting dispatch')}
            self.rows[token] = row
            return token, row

    def get(self, token, actor):
        with self.lock:
            self._expire()
            row = self.rows.get(token)
            if row is not None and row['actor'] != tuple(actor):
                raise PermissionError('transfer receipt belongs to another actor')
            return row
