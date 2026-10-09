import gzip
import json
from datetime import date

import pytest

from aptberg.config import Upstream
from aptberg.fetch import fetch_suite
from aptberg.manifest import Manifest, Ref
from aptberg.snapshot import CutError, base, next_id, pool_files, record

from .helpers import DAY, Archive, World, signer, source_stanza, stanza


def _lines(data: bytes) -> list[str]:
    return gzip.decompress(data).decode().splitlines()


def test_next_id():
    "First free letter of the day; other days do not count"
    assert next_id([], DAY) == '20260922a'
    assert next_id(['20260922a', '20260922b', '20260921c'], DAY) == (
        '20260922c'
    )
    with pytest.raises(CutError, match='26'):
        next_id([f'20260922{c}' for c in 'abcdefghijklmnopqrstuvwxyz'], DAY)


def test_pool_files_includes_source(tmp_path):
    "Every Filename a suite's Packages and Sources indexes name"
    archive = Archive()
    archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [
                stanza('app'),
            ]
        },
        sources=[source_stanza('app-src', 'app')],
    )
    up = Upstream(
        name='ubuntu',
        keyring=signer().keyring,
        components={'noble': ('main',)},
        architectures=('amd64',),
        url='http://up.example/ubuntu',
    )
    fs = fetch_suite(archive.client(), up, up.sources()[0], 'noble', tmp_path)
    assert pool_files(fs) == {
        'pool/main/a/app/app_1.0_amd64.deb',
        'pool/main/a/app-src/app-src_1.0.dsc',
        'pool/main/a/app-src/app-src_1.0.orig.tar.xz',
    }


def test_cut_refuses_missing_pool(tmp_path):
    "No snapshot names .debs that _pool/ lacks"
    w = World(tmp_path)
    with pytest.raises(CutError, match='not in _pool/'):
        w.cut(w.suite('noble'))
    assert w.store.objects == {}


def test_cut_layout(tmp_path):
    "Indexes, signatures and lists under the id; the marker comes last"
    w = World(tmp_path)
    w.sync_pool()
    w.store.puts.clear()
    fs = w.suite('noble')
    result = w.cut(fs)
    assert result.ref == Ref(None, '20260922a') and not result.reused
    root = '_snap/ubuntu/noble/20260922a/'
    assert base(fs) == '_snap/ubuntu/noble/'
    keys = sorted(k for k in w.store.objects if k.startswith(root))
    assert keys == [
        root + k
        for k in (
            'dists/noble/Contents-amd64.gz',
            'dists/noble/InRelease',
            'dists/noble/Release',
            'dists/noble/Release.gpg',
            'dists/noble/main/binary-amd64/Packages.gz',
            'dists/noble/main/binary-amd64/Packages.xz',
            'dists/noble/main/i18n/Translation-en.xz',
            'filenames.gz',
            'selected.gz',
            'snapshot.json',
        )
    ]
    assert w.store.puts[-1] == root + 'snapshot.json'
    signed = w.archive.files['/ubuntu/dists/noble/InRelease']
    assert w.store.objects[root + 'dists/noble/InRelease'][0] == signed
    assert _lines(w.store.objects[root + 'filenames.gz'][0]) == [
        'pool/main/a/app/app_1.0_amd64.deb',
        'pool/main/l/libfoo/libfoo_1.0_amd64.deb',
        'pool/main/u/unwanted/unwanted_1.0_amd64.deb',
    ]
    assert _lines(w.store.objects[root + 'selected.gz'][0]) == [
        'pool/main/a/app/app_1.0_amd64.deb',
        'pool/main/l/libfoo/libfoo_1.0_amd64.deb',
    ]
    marker = json.loads(w.store.objects[root + 'snapshot.json'][0])
    assert marker['release_sha256'] == fs.release_sha256
    assert marker['missing'] == []


def test_unchanged_reuses_id(tmp_path):
    "A Release equal to the latest snapshot's writes nothing"
    w = World(tmp_path)
    w.sync_pool()
    w.cut(w.suite('noble'))
    w.store.puts.clear()
    result = w.cut(w.suite('noble'), today=date(2026, 9, 23))
    assert result.reused and result.ref.id == '20260922a'
    assert w.store.puts == []


def test_moved_takes_next_letter(tmp_path):
    "An upstream that moved gets the next id; the old one is untouched"
    w = World(tmp_path)
    w.sync_pool()
    w.cut(w.suite('noble'))
    old = dict(w.store.objects)
    w.publish('1.1')
    w.sync_pool()
    assert w.cut(w.suite('noble')).ref.id == '20260922b'
    assert w.cut(w.suite('noble'), today=date(2026, 9, 23)).reused
    for key, value in old.items():
        assert w.store.objects[key] == value


def test_interrupted_cut_resumes(tmp_path):
    "A snapshot without its marker is completed under the same id"
    w = World(tmp_path)
    w.sync_pool()
    w.cut(w.suite('noble'))
    marker = '_snap/ubuntu/noble/20260922a/snapshot.json'
    del w.store.objects[marker]
    w.store.puts.clear()
    result = w.cut(w.suite('noble'))
    assert result.ref.id == '20260922a' and not result.reused
    assert result.uploaded == 0
    assert marker in w.store.objects


def test_allow_missing(tmp_path):
    "Missing pool files can be accepted, and are recorded"
    w = World(tmp_path)
    result = w.cut(w.suite('noble'), allow_missing=True)
    marker = json.loads(
        w.store.objects['_snap/ubuntu/noble/20260922a/snapshot.json'][0]
    )
    assert (
        marker['missing']
        == result.missing
        == [
            'pool/main/a/app/app_1.0_amd64.deb',
            'pool/main/l/libfoo/libfoo_1.0_amd64.deb',
        ]
    )


def test_dry_run(tmp_path):
    "A dry run picks the id and writes nothing"
    w = World(tmp_path)
    w.sync_pool()
    w.store.puts.clear()
    assert w.cut(w.suite('noble'), dry_run=True).ref.id == '20260922a'
    assert w.store.puts == []


def test_tree_snapshots_and_manifests(tmp_path):
    "Channel upstreams cut per tree; every acc following it is updated"
    w = World(tmp_path)
    w.sync_pool()
    fs = w.suite('anydist', '1.31')
    result = w.cut(fs)
    assert base(fs) == '_snap/kubernetes/1.31/anydist/'
    assert result.ref == Ref('1.31', '20260922a')
    root = tmp_path / 'manifests'
    paths = [m.path.name for m in record(result, root, 'bkt')]
    assert paths == [
        'experimental-cur.yaml',
        'experimental-acc.yaml',
        'stable-cur.yaml',
        'stable-acc.yaml',
    ]
    stable = Manifest.load(
        root, 'bkt', 'kubernetes', 'kubernetes', 'stable', 'acc'
    )
    assert stable.suites == {'anydist': Ref('1.31', '20260922a')}
    assert not (root / 'kubernetes' / 'verystable-acc.yaml').exists()

    only = record(
        w.cut(w.suite('anydist', '1.30')), root, 'bkt', channels=['stable']
    )
    assert only == []


def test_record_plain_upstream(tmp_path):
    "No channels: records into cur and acc, keeping other suites"
    w = World(tmp_path)
    w.sync_pool()
    root = tmp_path / 'manifests'
    acc = Manifest.load(root, 'bkt', 'ubuntu', 'ubuntu', None, 'acc')
    acc.suites['jammy'] = Ref(None, '20260715a')
    acc.save()
    record(w.cut(w.suite('noble')), root, 'bkt')
    assert (root / 'ubuntu' / 'acc.yaml').read_text() == (
        'prefix: s3://bkt/ubuntu/ch/acc\n'
        'suites:\n'
        '  jammy: 20260715a\n'
        '  noble: 20260922a\n'
    )
    assert (root / 'ubuntu' / 'cur.yaml').read_text() == (
        'prefix: s3://bkt/ubuntu/ch/cur\nsuites:\n  noble: 20260922a\n'
    )


def test_record_stages_restricts_which_are_touched(tmp_path):
    "stages=['cur'] writes only cur.yaml, never touching acc.yaml"
    w = World(tmp_path)
    w.sync_pool()
    root = tmp_path / 'manifests'
    (manifest,) = record(w.cut(w.suite('noble')), root, 'bkt', stages=['cur'])
    assert manifest.path.name == 'cur.yaml'
    assert (root / 'ubuntu' / 'cur.yaml').exists()
    assert not (root / 'ubuntu' / 'acc.yaml').exists()
