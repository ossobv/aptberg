"""Advisory locks: objects under _lock/, outside the served tree

    _lock/<upstream>/ch/<prefix>     apply and gc of that channel prefix
    _lock/fetch/<host>-<pid>         a fetch uploading into _pool/

They are advisory and racy: two processes starting at the same moment
can both see no lock. They guard against starting one job while another
runs, which the single serialized job running these should never do
anyway. A crashed job leaves its lock behind; --force breaks it.
"""

import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from .store import Store

LOCK = '_lock/'
FETCH = LOCK + 'fetch/'


class LockError(Exception):
    "Another job holds the lock"


def prefix_key(prefix: str) -> str:
    "The lock guarding a channel prefix: _lock/ubuntu/ch/acc"
    return LOCK + prefix.rstrip('/')


def _body() -> bytes:
    return json.dumps(
        {
            'host': socket.gethostname(),
            'pid': os.getpid(),
            'since': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
    ).encode()


@contextmanager
def exclusive(store: Store, key: str, force: bool = False) -> Iterator[None]:
    "Hold key; refuse if someone else holds it, unless force"
    held = store.get_bytes(key)
    if held is not None and not force:
        raise LockError(
            f'{key} is held: {held.decode(errors="replace")}; '
            f'--force if that job is gone'
        )
    store.put_bytes(key, _body(), '')
    try:
        yield
    finally:
        store.delete(key)


@contextmanager
def fetching(store: Store) -> Iterator[None]:
    "Mark a fetch as uploading into _pool/, for the pool pass of gc"
    key = f'{FETCH}{socket.gethostname()}-{os.getpid()}'
    store.put_bytes(key, _body(), '')
    try:
        yield
    finally:
        store.delete(key)
