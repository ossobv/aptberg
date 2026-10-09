from datetime import datetime, timedelta, timezone

import pytest

from aptberg import cli
from aptberg.apply import run
from aptberg.gc import pool_pass
from aptberg.manifest import Manifest, Ref
from aptberg.plan import build
from aptberg.retire import (
    RetireError,
    blockers,
    context,
    policy,
    retire,
    snapshots,
)

from .helpers import K8S, UP, move_noble, signer, two_suites

A, B = Ref(None, '20260922a'), Ref(None, '20260922b')
WEEK = timedelta(days=7)
ROOT_A = '_snap/ubuntu/noble/20260922a/'


def _now(**kw) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kw)


def _acc(w) -> Manifest:
    return Manifest.load(
        w.tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )


def _moved(tmp_path):
    "acc served noble a, then b; a is named by nothing any more"
    w = two_suites(tmp_path)
    run(build(_acc(w), UP, w.store), w.store)
    move_noble(w)
    run(build(_acc(w), UP, w.store), w.store)
    return w


def _snap(w, suite='noble', ref=A):
    return next(
        s for s in snapshots(w.store, UP) if s.suite == suite and s.ref == ref
    )


def _blockers(w, snap, now):
    manifests, served = context(
        w.store, UP, w.tmp_path / 'manifests', 'bkt', WEEK, now
    )
    return blockers(snap, manifests, served)


def test_snapshots(tmp_path):
    "Complete and incomplete trees, with and without a version tree"
    w = _moved(tmp_path)
    w.store.objects['_snap/ubuntu/noble/20260801a/dists/noble/x'] = (b'x', '')
    got = {(s.suite, str(s.ref), s.complete) for s in snapshots(w.store, UP)}
    assert got == {
        ('noble', '20260922a', True),
        ('noble', '20260922b', True),
        ('noble', '20260801a', False),
        ('noble-updates', '20260922a', True),
    }
    a = _snap(w)
    assert a.root == ROOT_A and a.cut_at is not None
    assert ROOT_A + 'snapshot.json' in a.keys and a.size > 0
    w.cut(w.suite('anydist', '1.31'))
    (k8s,) = snapshots(w.store, K8S)
    assert (k8s.suite, k8s.ref) == ('anydist', Ref('1.31', '20260922a'))


def test_blocked_while_named_or_recently_served(tmp_path):
    "A manifest naming it, or a prefix serving it lately, blocks retiring"
    w = two_suites(tmp_path)
    assert _blockers(w, _snap(w), _now()) == [
        f'named by {tmp_path / "manifests" / "ubuntu" / "acc.yaml"}',
        f'named by {tmp_path / "manifests" / "ubuntu" / "cur.yaml"}',
    ]
    w = _moved(tmp_path / 'moved')
    assert _blockers(w, _snap(w), _now()) == [
        'served by ubuntu/ch/acc/ within --grace'
    ]
    assert _blockers(w, _snap(w), _now(days=8)) == []
    assert _blockers(w, _snap(w, ref=B), _now(days=8)) != []


def test_retire_frees_the_pool(tmp_path):
    "Retiring deletes the tree, marker first; gc then frees its .debs"
    w = _moved(tmp_path)
    app = '_pool/_shared/main/a/app/app_1.0_amd64.deb'
    upstreams = {'ubuntu': UP}
    assert (
        app not in pool_pass(w.store, WEEK, upstreams, now=_now(days=30)).dead
    )
    snap = _snap(w)
    w.store.puts.clear()
    assert retire(w.store, snap) == len(snap.keys)
    assert w.store.puts[0] == '-' + ROOT_A + 'snapshot.json'
    assert not any(k.startswith(ROOT_A) for k in w.store.objects)
    assert app in pool_pass(w.store, WEEK, upstreams, now=_now(days=30)).dead


def test_policy(tmp_path):
    "--unused keeps each suite's newest and anything young"
    w = _moved(tmp_path)
    old = '_snap/ubuntu/noble/20260801a/dists/noble/x'
    w.store.objects[old] = (b'x', '')
    w.store.mtimes[old] = _now(days=-60)
    snaps = snapshots(w.store, UP)
    kept = policy(snaps, timedelta(days=30), _now())
    assert kept == {
        '_snap/ubuntu/noble/20260922b/': 'newest of its suite',
        '_snap/ubuntu/noble-updates/20260922a/': 'newest of its suite',
        ROOT_A: 'cut less than 30d ago',
    }
    kept = policy(snaps, timedelta(days=30), _now(days=31))
    assert ROOT_A not in kept
    assert '_snap/ubuntu/noble/20260801a/' not in kept  # old, incomplete


def _config(tmp_path, w, monkeypatch):
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: s\nbucket: bkt\nmanifests: manifests\nupstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n'
        f'    suites:\n      noble: [main]\n    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    return str(config)


def test_cli(tmp_path, monkeypatch, capsys):
    "One snapshot or the policy, of one upstream or all; dry run first"
    w = _moved(tmp_path)
    config = _config(tmp_path, w, monkeypatch)
    assert (
        cli.main(['-c', config, 'retire', 'ubuntu', 'noble', '20260922a']) == 1
    )
    assert 'served by ubuntu/ch/acc/' in capsys.readouterr().out
    monkeypatch.setattr('aptberg.retire.now_utc', lambda: _now(days=40))
    assert cli.main(['-c', config, 'retire', 'ubuntu', '--unused']) == 0
    out = capsys.readouterr().out
    assert f'would retire {ROOT_A}' in out
    assert 'keep    _snap/ubuntu/noble/20260922b/' in out
    assert ROOT_A + 'snapshot.json' in w.store.objects
    assert cli.main(['-c', config, 'retire', '--unused']) == 0
    assert f'would retire {ROOT_A}' in capsys.readouterr().out
    assert (
        cli.main(['-c', config, 'retire', 'ubuntu', '--unused', '--act']) == 0
    )
    assert f'retired {ROOT_A}' in capsys.readouterr().out
    assert ROOT_A + 'snapshot.json' not in w.store.objects


def test_cli_arguments(tmp_path, monkeypatch, caplog):
    "Either SUITE REF or --unused, not both, not neither"
    w = two_suites(tmp_path)
    config = _config(tmp_path, w, monkeypatch)
    for argv in (
        [],
        ['ubuntu'],
        ['ubuntu', 'noble'],
        ['ubuntu', 'noble', '20260922a', '--unused'],
    ):
        assert cli.main(['-c', config, 'retire', *argv]) == 1
    assert (
        cli.main(['-c', config, 'retire', 'ubuntu', 'noble', '20260101a']) == 1
    )
    assert 'no snapshot noble 20260101a' in caplog.text


def test_needs_manifests(tmp_path):
    "Without a manifests checkout nothing can be judged"
    w = two_suites(tmp_path)
    with pytest.raises(RetireError, match='manifests'):
        context(w.store, UP, None, 'bkt', WEEK, _now())
