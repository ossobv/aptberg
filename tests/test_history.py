import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from aptberg import cli
from aptberg.apply import run
from aptberg.history import (
    Event,
    HistoryError,
    Served,
    changes,
    events,
    parse_time,
    record,
    served_ever,
    state_at,
    verify,
)
from aptberg.manifest import Manifest, Ref
from aptberg.plan import build
from aptberg.promote import PromoteError, prepare

from .helpers import UP, move_noble, signer, two_suites

ACC = 'ubuntu/ch/acc/'
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
A, B = Ref(None, '20260922a'), Ref(None, '20260922b')


def _acc(w) -> Manifest:
    return Manifest.load(
        w.tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )


def _apply(w, manifest=None, **kw):
    return run(build(manifest or _acc(w), UP, w.store), w.store, **kw)


def test_apply_records(tmp_path):
    "An apply that changes what is served writes one event; a no-op none"
    w = two_suites(tmp_path)
    _apply(w, note='first')
    _apply(w)
    (event,) = events(w.store, ACC)
    assert event.note == 'first' and event.complete
    assert set(event.suites) == {'noble', 'noble-updates'}
    served = w.store.objects[ACC + 'dists/noble/InRelease'][0]
    assert event.suites['noble'] == Served(
        A, hashlib.sha256(served).hexdigest()
    )


def test_state_at_and_changes(tmp_path):
    "What was live at a moment, and since when"
    history = [
        Event(T0, ACC, {'noble': Served(A, 'a'), 'jammy': Served(A, 'j')}),
        Event(
            T0 + timedelta(days=7),
            ACC,
            {'noble': Served(B, 'b'), 'jammy': Served(A, 'j')},
        ),
        Event(
            T0 + timedelta(days=9),
            ACC,
            {'noble': Served(A, 'a')},
            complete=False,
        ),
    ]
    now = state_at(history)
    assert now['noble'].served.ref == A
    assert now['noble'].since == T0 + timedelta(days=9)
    assert now['jammy'].since == T0  # unchanged through two events
    mid = state_at(history, T0 + timedelta(days=8))
    assert mid['noble'].served.ref == B
    assert state_at(history, T0 - timedelta(seconds=1)) == {}
    assert [d for _, d in changes(history)] == [
        {'jammy': (None, A), 'noble': (None, A)},
        {'noble': (A, B)},
        {'noble': (B, A)},
    ]
    assert served_ever(history, 'noble', B) is history[1]
    assert served_ever(history, 'jammy', B) is None


def test_events_roundtrip_and_order(tmp_path):
    "Events are separate objects that sort by time"
    w = two_suites(tmp_path)
    late = record(
        w.store, ACC, {'noble': Served(B, 'b')}, at=T0 + timedelta(hours=1)
    )
    early = record(w.store, ACC, {'noble': Served(A, 'a')}, at=T0, note='x')
    assert early.key < late.key
    got = events(w.store, ACC)
    assert [e.suites['noble'].ref for e in got] == [A, B]
    assert got[0].note == 'x' and got[0].at == T0


def test_partial_release_recorded(tmp_path):
    "A release phase dying between suites records what went live"
    w = two_suites(tmp_path)
    real = w.store.copy

    def flaky(src, dst):
        if dst.endswith('noble-updates/InRelease'):
            raise OSError('boom')
        real(src, dst)

    w.store.copy = flaky
    with pytest.raises(OSError):
        _apply(w)
    (event,) = events(w.store, ACC)
    assert not event.complete
    assert list(event.suites) == ['noble']


def test_verify(tmp_path):
    "Recorded digests match the snapshots, and a forged one does not"
    w = two_suites(tmp_path)
    _apply(w)
    live = state_at(events(w.store, ACC))
    assert verify(w.store, {'ubuntu': UP}, live) == {}
    record(w.store, ACC, {'noble': Served(A, '0' * 64)})
    live = state_at(events(w.store, ACC))
    assert 'recorded 000' in verify(w.store, {'ubuntu': UP}, live)['noble']


def test_promote_older_ref(tmp_path):
    "A snapshot acc served before, but no longer, can still be promoted"
    w = two_suites(tmp_path)
    _apply(w)
    move_noble(w)
    _apply(w)
    assert _acc(w).suites['noble'] == B
    root = tmp_path / 'manifests'
    promotion = prepare(root, 'bkt', UP, None, ['noble'], w.store, ref=A)
    assert promotion.changes == {'noble': (None, A)}
    assert 'acc served it from' in promotion.evidence['noble']
    with pytest.raises(PromoteError, match='never served'):
        prepare(
            root,
            'bkt',
            UP,
            None,
            ['noble'],
            w.store,
            ref=Ref(None, '20260101a'),
        )
    with pytest.raises(PromoteError, match='exactly one'):
        prepare(root, 'bkt', UP, None, None, w.store, ref=A)


def test_parse_time():
    "Dates mean their end; naive times are UTC"
    assert parse_time('2026-09-22') == datetime(
        2026, 9, 22, 23, 59, 59, 999999, tzinfo=timezone.utc
    )
    assert parse_time('2026-09-22T10:00') == datetime(
        2026, 9, 22, 10, 0, tzinfo=timezone.utc
    )
    assert parse_time('2026-09-22T12:00+02:00').utcoffset() == timedelta(
        hours=2
    )
    assert parse_time('2026-09-22T10:00Z') == datetime(
        2026, 9, 22, 10, 0, tzinfo=timezone.utc
    )
    with pytest.raises(HistoryError):
        parse_time('yesterday')


def test_cli(tmp_path, monkeypatch, capsys):
    "aptberg history lists changes and answers --at, verified"
    w = two_suites(tmp_path)
    _apply(w)
    move_noble(w)
    _apply(w, note='second')
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: s\nbucket: bkt\nmanifests: manifests\nupstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n'
        f'    suites:\n      noble: [main]\n    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    assert cli.main(['-c', str(config), 'history', 'ubuntu/acc']) == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        'noble (new) -> 20260922a, noble-updates (new) -> 20260922a' in out[0]
    )
    assert 'noble 20260922a -> 20260922b' in out[1] and 'second' in out[1]
    assert (
        cli.main(
            [
                '-c',
                str(config),
                'history',
                'ubuntu/acc',
                '--at',
                'now',
                '--verify',
                '--suite',
                'noble',
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert 'noble: 20260922b since' in out and 'verified' in out
    assert 'noble-updates' not in out


def test_cli_json(tmp_path, monkeypatch, capsys):
    "history --json: one object per event, old and new ref per suite"
    w = two_suites(tmp_path)
    _apply(w)
    move_noble(w)
    _apply(w, note='second')
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: s\nbucket: bkt\nmanifests: manifests\nupstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n'
        f'    suites:\n      noble: [main]\n    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    args = ['-c', str(config), 'history', 'ubuntu/acc', '--json']
    assert cli.main([*args, '--suite', 'noble']) == 0
    got = json.loads(capsys.readouterr().out)
    assert [e['changes'] for e in got] == [
        {'noble': [None, '20260922a']},
        {'noble': ['20260922a', '20260922b']},
    ]
    assert got[1]['note'] == 'second' and got[1]['complete'] is True
