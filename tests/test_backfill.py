from dataclasses import replace
from datetime import datetime, timezone

import pytest

from aptberg import cli
from aptberg.apply import run
from aptberg.backfill import all_refs, live_refs, load
from aptberg.config import Channel, Config, Filter
from aptberg.manifest import Manifest, Ref
from aptberg.plan import PlanError, build
from aptberg.pool import select

from .helpers import UP, move_noble, signer, sync, two_suites

A, B = Ref(None, '20260922a'), Ref(None, '20260922b')
UNWANTED = '_pool/_shared/main/u/unwanted/unwanted_1.0_amd64.deb'
WIDE = replace(UP, filter=Filter(('app', 'unwanted')))


def _config(tmp_path, upstream=WIDE) -> Config:
    return Config(
        scratch=tmp_path / 'scratch',
        bucket='bkt',
        upstreams={'ubuntu': upstream},
        manifests=tmp_path / 'manifests',
    )


def _applied(tmp_path):
    w = two_suites(tmp_path)
    acc = Manifest.load(
        tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )
    run(build(acc, UP, w.store), w.store)
    return w


def _backfill(w, cfg, refs, dry_run=False):
    suites = load(w.store, cfg, refs, w.tmp_path / 'work')
    return sync(
        select(suites),
        w.store,
        w.archive.client(),
        w.tmp_path / 'tmp',
        dry_run=dry_run,
        progress=False,
    )


def test_live_refs(tmp_path):
    "Named by a manifest or served by a prefix"
    w = _applied(tmp_path)
    move_noble(w)  # acc.yaml now names b, serves a
    refs = live_refs(w.store, _config(tmp_path), datetime.now(timezone.utc))
    assert refs == {
        ('ubuntu', 'noble'): {A, B},
        ('ubuntu', 'noble-updates'): {A},
    }


def test_widened_filter_is_backfilled(tmp_path):
    "The new filter's packages for a live snapshot land in _pool/"
    w = _applied(tmp_path)
    assert UNWANTED not in w.store.objects
    cfg = _config(tmp_path)
    refs = live_refs(w.store, cfg, datetime.now(timezone.utc))
    dry = _backfill(w, cfg, refs, dry_run=True)
    assert dry.todo == 1 and UNWANTED not in w.store.objects
    result = _backfill(w, cfg, refs)
    assert (result.uploaded, result.failed) == (1, [])
    assert w.store.objects[UNWANTED][0] == b'unwanted 1.0 amd64'
    assert _backfill(w, cfg, refs).todo == 0


def test_same_filter_nothing_to_do(tmp_path):
    "Without a filter change, backfill finds everything present"
    w = _applied(tmp_path)
    cfg = _config(tmp_path, UP)
    refs = live_refs(w.store, cfg, datetime.now(timezone.utc))
    assert _backfill(w, cfg, refs).todo == 0


def test_deleted_upstream_is_reported(tmp_path):
    "A file upstream no longer has fails, and is reported"
    w = _applied(tmp_path)
    del w.archive.files['/ubuntu/pool/main/u/unwanted/unwanted_1.0_amd64.deb']
    cfg = _config(tmp_path)
    result = _backfill(
        w, cfg, live_refs(w.store, cfg, datetime.now(timezone.utc))
    )
    assert [k for k, _ in result.failed] == [UNWANTED]


def test_tampered_index_refused(tmp_path):
    "An index in _snap/ not matching its signed Release is refused"
    w = _applied(tmp_path)
    key = (
        '_snap/ubuntu/noble/20260922a/dists/noble/main/binary-amd64/'
        'Packages.xz'
    )
    w.store.objects[key] = (b'tampered', '')
    cfg = _config(tmp_path)
    with pytest.raises(PlanError, match='not as the Release says'):
        load(w.store, cfg, {('ubuntu', 'noble'): {A}}, tmp_path / 'work')


def test_override_source_url(tmp_path):
    "override_source_url uses each upstream's current url:, not marker"
    w = _applied(tmp_path)
    moved = replace(WIDE, url='http://up.example/mirror')
    cfg = _config(tmp_path, moved)
    refs = live_refs(w.store, cfg, datetime.now(timezone.utc))
    # Mirror every pool file under the new base, then drop the original:
    # only the current config's url can possibly satisfy the fetch.
    for path, data in list(w.archive.files.items()):
        if path.startswith('/ubuntu/pool/'):
            w.archive.files[path.replace('/ubuntu/', '/mirror/', 1)] = data
    del w.archive.files['/ubuntu/pool/main/u/unwanted/unwanted_1.0_amd64.deb']
    suites = load(
        w.store, cfg, refs, tmp_path / 'work', override_source_url=True
    )
    assert all(fs.source.url == 'http://up.example/mirror' for fs in suites)
    result = sync(
        select(suites),
        w.store,
        w.archive.client(),
        tmp_path / 'tmp',
        progress=False,
    )
    assert result.failed == []
    assert UNWANTED in w.store.objects


def test_override_source_url_needs_a_current_tree(tmp_path):
    "A ref whose tree the config no longer has cannot be resolved"
    w = _applied(tmp_path)
    refs = live_refs(w.store, _config(tmp_path), datetime.now(timezone.utc))
    # The live refs are all tree=None (no channels); reconfigure ubuntu
    # to only offer tree "1.0", as if the matching channel was dropped.
    channeled = replace(
        WIDE, url=None, channels={'stable': Channel('1.0', 'http://elsewhere')}
    )
    cfg = _config(tmp_path, channeled)
    with pytest.raises(PlanError, match='no current source for tree'):
        load(w.store, cfg, refs, tmp_path / 'work', override_source_url=True)


def test_all_refs(tmp_path):
    "--all-snapshots takes every complete snapshot"
    w = _applied(tmp_path)
    move_noble(w)
    assert all_refs(w.store, _config(tmp_path)) == {
        ('ubuntu', 'noble'): {A, B},
        ('ubuntu', 'noble-updates'): {A},
    }


def test_cli(tmp_path, monkeypatch, capsys):
    "aptberg backfill with a widened include list"
    w = _applied(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
        f'upstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble: [main]\n      noble-updates: [main]\n'
        f'    architectures: [amd64]\n'
        f'    filter:\n      include: [app, unwanted]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    monkeypatch.setattr('aptberg.sync.client', lambda *a: w.archive.client())
    assert cli.main(['-c', str(config), 'backfill', '--dry-run']) == 0
    assert '1 to upload' in capsys.readouterr().out
    assert cli.main(['-c', str(config), 'backfill']) == 0
    assert 'uploaded 1' in capsys.readouterr().out
    assert UNWANTED in w.store.objects
    assert not list((tmp_path / 'scratch').glob('backfill-*'))


def test_cli_override_source_url(tmp_path, monkeypatch, capsys):
    "--override-source-url needs no upstream named: it is per-upstream"
    w = _applied(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
        f'upstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble: [main]\n      noble-updates: [main]\n'
        f'    architectures: [amd64]\n'
        f'    filter:\n      include: [app, unwanted]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    monkeypatch.setattr('aptberg.sync.client', lambda *a: w.archive.client())
    assert (
        cli.main(['-c', str(config), 'backfill', '--override-source-url']) == 0
    )
    assert 'uploaded 1' in capsys.readouterr().out
    assert UNWANTED in w.store.objects
