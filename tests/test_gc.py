from datetime import datetime, timedelta, timezone

import pytest

from aptberg import cli
from aptberg.apply import run
from aptberg.gc import (
    GCError,
    collect,
    index_pass,
    pool_pass,
)
from aptberg.lock import FETCH, prefix_key
from aptberg.manifest import Manifest
from aptberg.plan import build

from .helpers import K8S, UP, move_noble, signer, two_suites

ACC = 'ubuntu/ch/acc/'
WEEK = timedelta(days=7)


def _now(**kw) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**kw)


def _acc(w) -> Manifest:
    return Manifest.load(
        w.tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )


def _flipped(tmp_path):
    "acc served snapshot a, then b; returns the world and a's by-hash keys"
    w = two_suites(tmp_path)
    run(build(_acc(w), UP, w.store), w.store)
    before = {
        k for k in w.store.objects if k.startswith(ACC) and '/by-hash/' in k
    }
    move_noble(w)
    run(build(_acc(w), UP, w.store), w.store)
    return w, before


def test_grace_keeps_what_was_just_served(tmp_path):
    "Right after a flip, the old snapshot's objects are all still live"
    w, _ = _flipped(tmp_path)
    found = index_pass(w.store, {'ubuntu': UP}, ACC, WEEK)
    assert found.dead == {}


def test_grace_runs_from_the_flip(tmp_path):
    "A grace after the flip, only what the old snapshot alone named dies"
    w, before = _flipped(tmp_path)
    found = index_pass(w.store, {'ubuntu': UP}, ACC, WEEK, now=_now(days=8))
    new = build(_acc(w), UP, w.store)
    assert new.ops == []
    # noble's old Packages by-hash objects: the only change between a and b
    dead = set(found.dead)
    assert dead and dead <= before
    assert all('/dists/noble/main/binary-amd64/by-hash/' in k for k in dead)
    # Everything the current snapshots name, and the Release files, stay.
    for suffix in (
        'InRelease',
        'Release',
        'Release.gpg',
        'main/binary-amd64/Packages.xz',
    ):
        assert f'{ACC}dists/noble/{suffix}' not in dead
    assert collect(w.store, found) == len(dead)
    assert (
        index_pass(w.store, {'ubuntu': UP}, ACC, WEEK, now=_now(days=8)).dead
        == {}
    )


def test_stale_canonical_file(tmp_path):
    "A canonical index no snapshot within the grace names is collected"
    w, _ = _flipped(tmp_path)
    stale = f'{ACC}dists/noble/main/binary-amd64/Packages.bz2'
    w.store.objects[stale] = (b'old', '')
    assert stale in index_pass(w.store, {'ubuntu': UP}, ACC, WEEK).dead


def test_no_history_refused(tmp_path):
    "A prefix serving files without history is not guessed at"
    w = two_suites(tmp_path)
    w.store.objects[f'{ACC}dists/noble/InRelease'] = (b'x', '')
    with pytest.raises(GCError, match='no history'):
        index_pass(w.store, {'ubuntu': UP}, ACC, WEEK)


def test_snapshot_gone_refused(tmp_path):
    "A snapshot served within the grace must still exist"
    w, _ = _flipped(tmp_path)
    del w.store.objects['_snap/ubuntu/noble/20260922a/snapshot.json']
    with pytest.raises(GCError, match='is gone'):
        index_pass(w.store, {'ubuntu': UP}, ACC, WEEK)


def test_retired_prefix_is_all_dead(tmp_path):
    "A prefix the config no longer names dies whole, even when current"
    w = two_suites(tmp_path)
    run(build(_acc(w), UP, w.store), w.store)
    live = {k for k in w.store.objects if k.startswith(ACC)}
    assert live
    found = index_pass(w.store, {'ubuntu': UP}, ACC, WEEK)
    assert found.dead == {} and found.kept == len(live)
    old = 'ubuntu/ch/verystable-acc/'
    w.store.objects[f'{old}dists/noble/InRelease'] = (b'x', '')
    w.store.objects[f'{old}dists/noble/main/Packages.gz'] = (b'y', '')
    kept = index_pass(w.store, {'ubuntu': UP}, old, WEEK)
    assert kept.unconfigured and kept.dead == {}
    found = index_pass(
        w.store, {'ubuntu': UP}, old, WEEK, drop_unconfigured=True
    )
    assert set(found.dead) == {k for k in w.store.objects if k.startswith(old)}
    assert collect(w.store, found) == 2
    assert not any(k.startswith(old) for k in w.store.objects)
    assert {k for k in w.store.objects if k.startswith(ACC)} == live


def test_channel_prefix_retired_with_its_channel(tmp_path):
    "<channel>-<stage> is live only while the channel is configured"
    w = two_suites(tmp_path)
    for name, retired in (
        ('stable-acc', False),
        ('stable-prod', False),
        ('early-acc', True),
        ('stable', True),
        ('acc', True),
    ):
        prefix = f'kubernetes/ch/{name}/'
        w.store.objects[f'{prefix}dists/anydist/InRelease'] = (b'x', '')
        try:
            found = index_pass(
                w.store,
                {'kubernetes': K8S},
                prefix,
                WEEK,
                drop_unconfigured=True,
            )
        except GCError:
            assert not retired  # configured, but has no history here
        else:
            assert retired and len(found.dead) == 1


def test_pool_pass(tmp_path):
    "Pool files live while any snapshot names them, retired or not"
    w, _ = _flipped(tmp_path)
    orphan = '_pool/_shared/main/o/orphan/orphan_1.0_amd64.deb'
    w.store.objects[orphan] = (b'o', '')
    young = '_pool/_shared/main/y/young/young_1.0_amd64.deb'
    w.store.objects[young] = (b'y', '')
    w.store.mtimes[young] = _now(days=30)
    found = pool_pass(w.store, WEEK, {'ubuntu': UP}, now=_now(days=30))
    # kubelet was fetched into the pool but never cut: dead after the grace.
    assert set(found.dead) == {
        orphan,
        '_pool/_shared/main/k/kubelet/kubelet_1.30.0_amd64.deb',
        '_pool/_shared/main/k/kubelet/kubelet_1.31.0_amd64.deb',
    }
    assert found.young == 1
    # app 1.0 is only in snapshot a; a is not served but still exists.
    assert '_pool/_shared/main/a/app/app_1.0_amd64.deb' not in found.dead
    # Retiring a (dropping its tree) makes its own files dead.
    for key in [
        k
        for k in w.store.objects
        if k.startswith('_snap/ubuntu/noble/20260922a/')
    ]:
        del w.store.objects[key]
    found = pool_pass(w.store, WEEK, {'ubuntu': UP}, now=_now(days=30))
    assert '_pool/_shared/main/a/app/app_1.0_amd64.deb' in found.dead
    assert '_pool/_shared/main/a/app/app_2.0_amd64.deb' not in found.dead


def test_pool_pass_refuses_while_fetching(tmp_path):
    "A fetch marker stops the pool pass; sync writes and removes one"
    w = two_suites(tmp_path)
    assert any(k.startswith(FETCH) for k in w.store.puts)
    assert not any(k.startswith(FETCH) for k in w.store.objects)
    w.store.objects[FETCH + 'host-1'] = (b'{}', '')
    with pytest.raises(GCError, match='a fetch is uploading'):
        pool_pass(w.store, WEEK, {'ubuntu': UP})
    pool_pass(w.store, WEEK, {'ubuntu': UP}, force=True)


def test_cli(tmp_path, monkeypatch, capsys):
    "Dry run by default; --act takes the prefix lock and deletes"
    w, _ = _flipped(tmp_path)
    stale = f'{ACC}dists/noble/main/binary-amd64/Packages.bz2'
    w.store.objects[stale] = (b'old', '')
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: s\nbucket: bkt\nupstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble: [main]\n    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    assert cli.main(['-c', str(config), 'gc', '--list']) == 0
    out = capsys.readouterr().out
    assert f'{ACC}: would delete 1 ' in out and stale in out
    assert '_pool/: would delete 0 ' in out
    assert stale in w.store.objects

    # A held prefix lock (an apply running) stops the deleting run.
    w.store.objects[prefix_key(ACC)] = (b'{"host": "busy"}', '')
    assert cli.main(['-c', str(config), 'gc', 'ubuntu/acc', '--act']) == 1
    assert stale in w.store.objects
    del w.store.objects[prefix_key(ACC)]
    assert cli.main(['-c', str(config), 'gc', 'ubuntu/acc', '--act']) == 0
    assert stale not in w.store.objects
    assert prefix_key(ACC) not in w.store.objects
