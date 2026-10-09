import json
from datetime import datetime, timedelta, timezone

import pytest

from aptberg import cli
from aptberg.apply import run
from aptberg.manifest import Manifest, Ref
from aptberg.plan import build
from aptberg.promote import prepare
from aptberg.status import cells

from .helpers import UP, group_world, move_noble, signer, two_suites

A, B = Ref(None, '20260922a'), Ref(None, '20260922b')


def _now(**kw) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kw)


def _cells(w, upstream=UP, **kw):
    kw.setdefault('now', _now())
    return {
        (c.name, c.suite): c
        for c in cells(
            w.store, upstream, w.tmp_path / 'manifests', 'bkt', **kw
        )
    }


def _acc(w) -> Manifest:
    return Manifest.load(
        w.tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )


def test_ok(tmp_path):
    "Applied acc: named and served agree, with a since and upstream date"
    w = group_world(tmp_path)
    got = _cells(w)
    assert set(got) == {
        ('acc', 'noble'),
        ('acc', 'noble-updates'),
        ('cur', 'noble'),
        ('cur', 'noble-updates'),
    }
    cell = got['acc', 'noble']
    assert (cell.manifest, cell.served, cell.states) == (A, A, [])
    assert cell.since is not None
    assert cell.release_date == 'Tue, 22 Sep 2026 08:00:00 UTC'
    assert cell.prefix == 'ubuntu/ch/acc/' and cell.stage == 'acc'


def test_pending_after_cut(tmp_path):
    "A cut recorded in acc but not applied is pending"
    w = group_world(tmp_path)
    move_noble(w)
    cell = _cells(w)['acc', 'noble']
    assert (cell.manifest, cell.served) == (B, A)
    assert cell.states == ['pending'] and not cell.problem


def test_behind_acc(tmp_path):
    "prod serving an older ref than acc is information, not a problem"
    w = group_world(tmp_path)
    promotion = prepare(tmp_path / 'manifests', 'bkt', UP, None, None, w.store)
    promotion.prod.save()
    run(build(promotion.prod, UP, w.store), w.store)
    move_noble(w)
    run(build(_acc(w), UP, w.store), w.store)
    got = _cells(w)
    assert got['prod', 'noble'].states == ['behind acc']
    assert got['prod', 'noble-updates'].states == []
    assert got['acc', 'noble'].states == []


def test_drift_and_unrecorded(tmp_path):
    "Bytes changed behind aptberg's back, and suites nobody recorded"
    w = group_world(tmp_path)
    key = 'ubuntu/ch/acc/dists/noble/InRelease'
    w.store.objects[key] = (b'forged', 'f' * 64)
    w.store.objects['ubuntu/ch/acc/dists/jammy/InRelease'] = (b'x', 'e')
    got = _cells(w)
    assert got['acc', 'noble'].states == ['drift']
    assert got['acc', 'jammy'].states == ['unrecorded']
    assert got['acc', 'jammy'].served is None
    assert got['acc', 'noble'].problem


def test_incomplete(tmp_path):
    "Suites a partial apply did not reach are flagged"
    w = two_suites(tmp_path)
    real = w.store.copy

    def flaky(src, dst):
        if dst.endswith('noble-updates/InRelease'):
            raise OSError('boom')
        real(src, dst)

    w.store.copy = flaky
    with pytest.raises(OSError):
        run(build(_acc(w), UP, w.store), w.store)
    got = _cells(w)
    assert got['acc', 'noble'].states == []
    assert 'incomplete' in got['acc', 'noble-updates'].states


def _config(tmp_path, w, monkeypatch):
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
        f'upstreams:\n'
        f'  ubuntu:\n    group: ubuntu\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n'
        f'    suites:\n      noble: [main]\n      noble-updates: [main]\n'
        f'    architectures: [amd64]\n'
        f'  ubuntu-security:\n    group: ubuntu\n'
        f'    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n'
        f'    suites:\n      noble-security: [main]\n'
        f'    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    return str(config)


def test_cli(tmp_path, monkeypatch, capsys):
    "A table for people, JSON for monitoring, exit 1 on problems"
    w = group_world(tmp_path)
    config = _config(tmp_path, w, monkeypatch)
    assert cli.main(['-c', config, 'status']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ['PREFIX', 'SUITE', 'MANIFEST', 'SERVED']
    assert any(
        line.startswith('ubuntu-security/ch/acc  noble-security')
        for line in lines
    )
    # cur is recorded on every cut just like acc, but only acc was
    # applied here.
    assert all(
        line.endswith('ok')
        for line in lines[1:]
        if not line.split()[0].endswith('/cur')
    )
    assert all(
        line.endswith('pending')
        for line in lines[1:]
        if line.split()[0].endswith('/cur')
    )

    assert cli.main(['-c', config, 'status', 'ubuntu', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert {d['suite'] for d in data} == {
        'noble',
        'noble-updates',
        'noble-security',
    }
    served = next(d for d in data if d['age_seconds'] is not None)
    assert served['age_seconds'] >= 0

    w.store.objects['ubuntu/ch/acc/dists/noble/InRelease'] = (b'x', '0')
    assert cli.main(['-c', config, 'status']) == 1
    assert 'drift' in capsys.readouterr().out
