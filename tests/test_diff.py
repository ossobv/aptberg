import hashlib
from dataclasses import replace

import pytest

from aptberg.apply import run
from aptberg.diff import (
    DiffError,
    Pair,
    backwards,
    diff_suite,
    new_files,
    pairs,
    sides,
)
from aptberg.fetch import fetch_suite
from aptberg.manifest import Manifest, ManifestError, Ref
from aptberg.plan import build
from aptberg.pool import select
from aptberg.promote import units
from aptberg.snapshot import record

from .helpers import SEC, UP, UPG, World, group_world, stanza


def _recut_noble(w, packages):
    "Republish noble's content, refetch, resync and cut it again"
    w.archive.add_suite('/ubuntu', 'noble', {'main/binary-amd64': packages})
    w.suites[0] = fetch_suite(
        w.archive.client(),
        UP,
        UP.sources()[0],
        'noble',
        w.tmp_path / 'scratch',
    )
    w.selection = select(w.suites)
    w.sync_pool()
    return w.cut(w.suite('noble'))


def test_diff_added_removed_changed(tmp_path):
    "Package added, one removed, one with a bumped version"
    w = World(tmp_path)
    w.sync_pool()
    ref_a = w.cut(w.suite('noble')).ref
    assert ref_a == Ref(None, '20260922a')

    ref_b = _recut_noble(
        w,
        [
            stanza('app', '2.0', Depends='libfoo'),
            stanza('libfoo'),
            stanza('newpkg'),
        ],
    ).ref
    assert ref_b == Ref(None, '20260922b')

    result = diff_suite(w.store, 'ubuntu', 'noble', ref_a, ref_b)
    assert list(result) == ['main/binary-amd64']
    d = result['main/binary-amd64']
    assert d.added == {'newpkg': '1.0'}
    assert d.removed == {'unwanted': '1.0'}
    assert d.changed == {'app': ('1.0', '2.0')}


def test_diff_no_changes(tmp_path):
    "An unchanged suite reuses its id and diffs empty against itself"
    w = World(tmp_path)
    w.sync_pool()
    ref_a = w.cut(w.suite('noble')).ref
    result = w.cut(w.suite('noble'))
    assert result.reused and result.ref == ref_a
    assert diff_suite(w.store, 'ubuntu', 'noble', ref_a, result.ref) == {}


def test_diff_missing_snapshot(tmp_path):
    "A snapshot id that was never cut is refused, not silently empty"
    w = World(tmp_path)
    w.sync_pool()
    ref_a = w.cut(w.suite('noble')).ref
    with pytest.raises(DiffError, match='no such snapshot'):
        diff_suite(w.store, 'ubuntu', 'noble', ref_a, Ref(None, '20260101a'))


def _apply(w, up, stage):
    "Apply up's stage manifest: the prefix serves it, history says so"
    manifest = Manifest.load(
        w.tmp_path / 'manifests', 'bkt', up.name, up.served, None, stage
    )
    run(build(manifest, up, w.store), w.store)


def _security_cur(w, packages, apply=True):
    "Cut a new noble-security with these packages, recorded as cur"
    w.archive.add_suite(
        '/ubuntu',
        'noble-security',
        {'main/binary-amd64': packages},
        codename='noble',
    )
    sec = fetch_suite(
        w.archive.client(),
        SEC,
        SEC.sources()[0],
        'noble-security',
        w.tmp_path / 'scratch',
    )
    w.suites[-1] = sec
    w.selection = select(w.suites)
    w.sync_pool()
    record(w.cut(sec), w.tmp_path / 'manifests', 'bkt', stage='cur')
    if apply:
        _apply(w, SEC, 'cur')


def _pairs(w, text, **kw):
    a, b = sides(text)
    return pairs(
        w.store,
        {u.name: u for u in (UPG, SEC)},
        units([UPG, SEC]),
        a,
        b,
        **kw,
    )


def test_sides():
    "A..B: stages or refs, nothing else"
    assert sides('prod..cur') == ('prod', 'cur')
    assert sides('1.31/20261012b..cur') == (Ref('1.31', '20261012b'), 'cur')
    for bad in ('prod', 'prod..', '..cur', 'prod...cur', 'prod..cur..acc'):
        with pytest.raises(DiffError):
            sides(bad)
    with pytest.raises(ManifestError):
        sides('prod..nosuch')


def test_backwards():
    "A newer stage first runs backwards; refs never do"
    assert backwards('cur', 'acc') and backwards('acc', 'prod')
    assert not backwards('prod', 'acc') and not backwards('acc', 'cur')
    assert not backwards('acc', 'acc')
    assert not backwards(Ref(None, '20261022a'), 'prod')


def test_pairs_of_stages(tmp_path, caplog):
    "Each suite's served ref; equal refs left out; nothing served skipped"
    w = group_world(tmp_path)
    assert _pairs(w, 'acc..acc') == []
    _security_cur(w, [stanza('app', '1.0.3', Depends='libfoo')])
    got = _pairs(w, 'acc..cur')
    assert [(p.upstream.name, p.suite) for p in got] == [
        ('ubuntu-security', 'noble-security')
    ]
    assert got[0].a != got[0].b and got[0].prefix == 'ubuntu-security/ch/cur/'
    # ubuntu's cur serves nothing: not "every package removed".
    assert 'ubuntu/ch/cur serves nothing; skipped' in caplog.text


def test_pairs_served_not_manifest(tmp_path):
    "A manifest cut but not applied yet does not count"
    w = group_world(tmp_path)
    _security_cur(w, [stanza('app', '1.0.3', Depends='libfoo')])
    (applied,) = _pairs(w, 'acc..cur')
    _security_cur(w, [stanza('app', '1.0.4', Depends='libfoo')], apply=False)
    assert _pairs(w, 'acc..cur') == [applied]


def test_pairs_drift(tmp_path):
    "A prefix serving another Release than its history records is refused"
    w = group_world(tmp_path)
    key = 'ubuntu-security/ch/acc/dists/noble-security/InRelease'
    w.store.put_bytes(key, b'tampered', 'f' * 64)
    with pytest.raises(DiffError, match='another Release'):
        _pairs(w, 'acc..acc')


def test_pairs_codename(tmp_path):
    "--codename finds every suite of the release, across the group"
    w = group_world(tmp_path)
    _security_cur(w, [stanza('app', '1.0.3', Depends='libfoo')])
    got = _pairs(w, 'acc..cur', codenames=['noble'])
    assert [p.suite for p in got] == ['noble-security']


def test_pairs_refused(tmp_path):
    "Nothing to compare, or a typo, is an error: not 'no differences'"
    w = group_world(tmp_path)
    with pytest.raises(DiffError, match='no suite to compare'):
        _pairs(w, 'acc..prod')
    with pytest.raises(DiffError, match='--suite'):
        _pairs(w, 'acc..20260922a')
    with pytest.raises(DiffError, match='--codename needs a stage'):
        _pairs(w, '20260922a..20260922b', codenames=['noble'])
    with pytest.raises(DiffError, match='no such suite: nosuch'):
        _pairs(w, 'acc..acc', suites=['nosuch'])


def test_new_files(tmp_path):
    "The files cur names and acc does not, by source, with their hash"
    w = group_world(tmp_path)
    php = [
        stanza('php8.5-cli', '8.5.4-1', Source='php8.5'),
        stanza('libphp8.5-embed', '8.5.4-1', Source='php8.5 (8.5.4-1)'),
        stanza('php-common', '1', arch='all'),
    ]
    _security_cur(w, [stanza('app', '1.0.2', Depends='libfoo'), *php])
    (pair,) = _pairs(w, 'acc..cur', suites=['noble-security'])

    got = new_files(w.store, pair)
    # app 1.0.2 was already in acc: not new.
    assert [f.package for f in got] == [
        'libphp8.5-embed',
        'php-common',
        'php8.5-cli',
    ]
    cli_deb = got[2]
    assert cli_deb.codename == 'noble' and cli_deb.component == 'main'
    assert cli_deb.source == 'php8.5' and cli_deb.version == '8.5.4-1'
    assert cli_deb.path == (
        'ubuntu-security/ch/cur/pool/main/p/php8.5-cli/'
        'php8.5-cli_8.5.4-1_amd64.deb'
    )
    content = b'php8.5-cli 8.5.4-1 amd64'
    assert cli_deb.size == len(content)
    assert cli_deb.sha256 == hashlib.sha256(content).hexdigest()

    php_only = new_files(w.store, pair, ['php8*'])
    assert [f.package for f in php_only] == ['libphp8.5-embed', 'php8.5-cli']
    d = diff_suite(
        w.store, 'ubuntu-security', 'noble-security', pair.a, pair.b, ['php8*']
    )
    assert d['main/binary-amd64'].added == {
        'libphp8.5-embed': '8.5.4-1',
        'php8.5-cli': '8.5.4-1',
    }


def test_new_files_missing_side(tmp_path):
    "A suite one side lacks: everything the other side names is new"
    w = group_world(tmp_path)
    pair = Pair(SEC, None, 'noble-security', None, Ref(None, '20260922a'))
    assert [f.package for f in new_files(w.store, pair)] == ['app']
    assert new_files(w.store, replace(pair, a=pair.b, b=None)) == []
