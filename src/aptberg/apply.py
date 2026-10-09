"""Apply: execute a plan against its channel prefix

Phases run in order and each one finishes before the next starts. The
barrier is the correctness property: a release that lands before its
by-hash objects hands clients hashes that do not resolve. Do not merge
the phases into one pool.

The release phase runs sequentially, suite by suite (InRelease, Release,
Release.gpg), so the detached pair is out of step for as short a time as
possible. Afterwards the served state is recorded in _history/; if the
release phase fails part-way, the suites that did go live are recorded,
marked incomplete.

A crash part-way is safe: nothing before the release phase is observable
to clients, and rerunning plans only what did not land.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .history import Served, record
from .lock import exclusive, prefix_key
from .plan import PHASES, Op, Plan
from .store import Store

log = logging.getLogger(__name__)


def run(
    plan: Plan,
    store: Store,
    concurrency: int = 8,
    force: bool = False,
    note: str = 'apply',
) -> int:
    """Apply a plan; return the number of ops done

    The prefix is locked (see lock.py) against a second apply or a gc of
    the same prefix; force breaks a stale lock.
    """
    if not plan.ops:
        return 0
    done = 0
    with exclusive(store, prefix_key(plan.prefix), force):
        for phase in PHASES[:-1]:
            ops = plan.by_phase(phase)
            if ops:
                done += _phase(ops, store, concurrency)
                log.info('%s: %s: %d ops', plan.prefix, phase, len(ops))
        done += _release(plan, store, note)
    return done


def _release(plan: Plan, store: Store, note: str) -> int:
    """The release phase, suite by suite, then the history event

    An apply without release ops still changed what the prefix serves
    (a snapshot of the same Release with more index files, dep11 added
    say), so it is recorded too.
    """
    ops = plan.by_phase('release')
    if not ops:
        _record(plan, store, set(), note)
        return 0
    last = {op.suite: i for i, op in enumerate(ops)}
    pending = set(last)
    try:
        for i, op in enumerate(ops):
            _one(op, store)
            if last[op.suite] == i:
                pending.discard(op.suite)
    except BaseException:
        _record(plan, store, pending, note)
        raise
    _record(plan, store, set(), note)
    log.info('%s: release: %d ops', plan.prefix, len(ops))
    return len(ops)


def _record(plan: Plan, store: Store, pending: set[str], note: str) -> None:
    """Record what the prefix serves now; pending suites did not flip

    A pending suite may still serve its previous snapshot, or be half
    way; it is left out rather than guessed at.
    """
    suites = {}
    for suite, ref in plan.suites.items():
        if suite in pending:
            continue
        sigs = plan.signatures[suite]
        sha256 = sigs.get('InRelease') or sigs.get('Release', '')
        suites[suite] = Served(ref, sha256)
    try:
        event = record(
            store, plan.prefix, suites, complete=not pending, note=note
        )
    except Exception as exc:  # the apply itself is done; do not hide it
        log.error('%s: could not record history: %s', plan.prefix, exc)
    else:
        log.info('%s: recorded %s', plan.prefix, event.key)


def _phase(ops: list[Op], store: Store, workers: int) -> int:
    if workers == 1:
        for op in ops:
            _one(op, store)
        return len(ops)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_one, op, store) for op in ops]
        try:
            for future in as_completed(futures):
                future.result()
        except BaseException:
            # The first failure ends the phase: no later phase may run.
            for future in futures:
                future.cancel()
            raise
    return len(ops)


def _one(op: Op, store: Store) -> None:
    if op.kind == 'copy':
        store.copy(op.src, op.dst)
    elif op.kind == 'delete':
        store.delete(op.dst)
    else:
        raise ValueError(f'unknown op kind {op.kind!r}')
