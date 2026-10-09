"""A flat upstream (NVIDIA's CUDA repos): Release and debs at the root

Nothing is re-signed, so the Release must reach clients byte for byte,
at the top of the channel prefix, with its ./x.deb Filenames served
through the pool rewrite. Tested end to end, through a real fetch, cut
and apply, since the layout touches every reader of a served prefix.
"""

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from aptberg.apply import run
from aptberg.config import Config, ConfigError, load
from aptberg.fetch import fetch_suite
from aptberg.gc import index_pass
from aptberg.history import events
from aptberg.manifest import Ref
from aptberg.plan import build
from aptberg.pool import PREFIX, PoolConflict, select, storage_key
from aptberg.promote import prepare
from aptberg.snapshot import cut, record
from aptberg.status import cells
from aptberg.verify import verify_http, verify_prefix

from .helpers import DAY, FLAT, Archive, FakeStore, signer, stanza, sync

ACC = 'nvidia/ch/acc/'
REWRITE = re.compile(r'^([^/]+/ch/[^/]+/)([^/]+\.deb)$')


class Flat:
    "An archive with a flat repository, fetched, synced, cut and applied"

    def __init__(self, tmp_path, upstream=FLAT, by_hash=False):
        self.tmp_path = tmp_path
        self.upstream = upstream
        self.archive = Archive()
        self.store = FakeStore()
        self.by_hash = by_hash
        self.release = self.publish('1.0')
        self.cut_and_apply()

    def publish(self, version):
        return self.archive.add_flat(
            '/cuda',
            [stanza('app', version), stanza('libfoo')],
            by_hash=self.by_hash,
        )

    def cut_and_apply(self, today=DAY):
        http = self.archive.client()
        (source,) = self.upstream.sources()
        self.fs = fetch_suite(
            http, self.upstream, source, 'nvidia', self.tmp_path / 'scratch'
        )
        selection = select([self.fs])
        sync(
            selection, self.store, http, self.tmp_path / 'tmp', progress=False
        )
        result = cut(
            self.fs,
            selection,
            self.store,
            self.store.list_sizes(PREFIX),
            today=today,
        )
        self.manifests = record(result, self.tmp_path / 'manifests', 'bkt')
        self.acc = next(m for m in self.manifests if m.path.stem == 'acc')
        run(build(self.acc, self.upstream, self.store), self.store)


def test_fetch_takes_what_the_release_lists(tmp_path):
    "No suite, codename or components to match; the family is configured"
    w = Flat(tmp_path)
    assert sorted(e.path for e in w.fs.entries) == ['Packages.gz']
    assert w.fs.release.codename == ''
    assert w.fs.family == 'noble'
    assert w.fs.path == tmp_path / 'scratch' / 'nvidia' / 'nvidia'
    assert '/cuda/dists/nvidia/Release' not in w.archive.requests


def test_family_without_codename(tmp_path):
    "No codename: the suite is its own family, joining every one"
    w = Flat(tmp_path, upstream=replace(FLAT, codename=None))
    assert w.fs.family == 'nvidia'


def test_pool_keys_and_urls(tmp_path):
    "./x.deb lands in _pool/<pool>/x.deb and downloads from beside Release"
    w = Flat(tmp_path)
    assert storage_key(FLAT, './app_1.0_amd64.deb') == (
        '_pool/nvidia/app_1.0_amd64.deb'
    )
    assert '_pool/nvidia/app_1.0_amd64.deb' in w.store.objects
    assert '/cuda/app_1.0_amd64.deb' in w.archive.requests


def test_filename_must_stay_below_the_repository(tmp_path):
    "A flat Filename outside ./ or climbing out of it is refused"
    for bad in (
        'pool/app_1.0_amd64.deb',
        './../app_1.0_amd64.deb',
        './a/../../app_1.0_amd64.deb',
        '/etc/app.deb',
    ):
        w = Flat.__new__(Flat)
        w.tmp_path, w.upstream = tmp_path / 'bad', FLAT
        w.archive, w.store = Archive(), FakeStore()
        text, content = stanza('app')
        text = text.replace(
            'Filename: pool/main/a/app/app_1.0_amd64.deb', f'Filename: {bad}'
        )
        w.archive.add_flat('/cuda', [(text, content)])
        (source,) = FLAT.sources()
        fs = fetch_suite(
            w.archive.client(), FLAT, source, 'nvidia', w.tmp_path / 'scratch'
        )
        with pytest.raises(PoolConflict):
            select([fs])


def test_served_at_the_prefix_root_byte_identical(tmp_path):
    "Release and index files at the prefix top, no dists/, as upstream"
    w = Flat(tmp_path)
    keys = {k for k in w.store.objects if k.startswith(ACC)}
    (entry,) = w.fs.entries
    assert keys == {
        ACC + n
        for n in (
            'InRelease',
            'Release',
            'Release.gpg',
            'Packages.gz',
            f'by-hash/SHA256/{entry.sha256}',
        )
    }
    for name in ('InRelease', 'Release', 'Release.gpg', 'Packages.gz'):
        assert (
            w.store.objects[ACC + name][0]
            == (w.archive.files['/cuda/' + name])
        )
    assert w.store.objects[ACC + 'Release'][0].decode() == w.release


def test_by_hash_beside_the_release(tmp_path):
    "A Release promising by-hash gets it at <prefix>by-hash/SHA256/"
    w = Flat(tmp_path, by_hash=True)
    digest = next(e.sha256 for e in w.fs.entries)
    assert ACC + f'by-hash/SHA256/{digest}' in w.store.objects


def test_replan_is_empty_and_a_move_replaces(tmp_path):
    "Applying twice does nothing; a new Release flips the prefix top"
    w = Flat(tmp_path)
    assert build(w.acc, FLAT, w.store).ops == []
    old = w.store.objects[ACC + 'InRelease'][0]
    w.publish('2.0')
    w.cut_and_apply(today=DAY + timedelta(days=1))
    assert w.store.objects[ACC + 'InRelease'][0] != old
    assert w.acc.suites['nvidia'] == Ref(None, '20260923a')
    assert build(w.acc, FLAT, w.store).ops == []
    assert [e.suites['nvidia'].ref for e in events(w.store, ACC)] == [
        Ref(None, '20260922a'),
        Ref(None, '20260923a'),
    ]


def test_verify_prefix(tmp_path):
    "The suite is found without dists/, and its indexes and pool checked"
    w = Flat(tmp_path, by_hash=True)
    (report,) = verify_prefix(w.store, {'nvidia': FLAT}, ACC, deep=True)
    assert report.suite == 'nvidia'
    assert report.errors == []
    assert (report.indexes, report.pool) == (2, 2)
    del w.store.objects[ACC + 'Packages.gz']
    (report,) = verify_prefix(w.store, {'nvidia': FLAT}, ACC)
    assert any('Packages.gz' in e for e in report.errors)


def _front_end(w, rewrite=True) -> httpx.Client:
    "The bucket over HTTP, with <prefix>x.deb rewritten into the pool"

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip('/')
        if rewrite:
            key = REWRITE.sub(r'_pool/nvidia/\2', key)
        if key not in w.store.objects:
            return httpx.Response(404)
        data = w.store.objects[key][0]
        if request.method == 'HEAD':
            return httpx.Response(
                200, headers={'content-length': str(len(data))}
            )
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_verify_http(tmp_path):
    "Over HTTP: Release at the top, by-hash, and .debs through the rewrite"
    w = Flat(tmp_path, by_hash=True)
    (report,) = verify_http(
        _front_end(w), 'http://aptberg.test', {'nvidia': FLAT}, ACC, w.store
    )
    assert report.errors == []
    assert (report.indexes, report.pool) == (1, 2)
    (report,) = verify_http(
        _front_end(w, rewrite=False),
        'http://aptberg.test',
        {'nvidia': FLAT},
        ACC,
        w.store,
    )
    assert len(report.errors) == 2
    assert all(e.endswith('HTTP 404') for e in report.errors)


def test_status(tmp_path):
    "The suite shows as served and in step with its manifest"
    w = Flat(tmp_path)
    rows = cells(
        w.store,
        FLAT,
        tmp_path / 'manifests',
        'bkt',
        datetime.now(timezone.utc),
    )
    acc = next(c for c in rows if c.name == 'acc')
    assert (acc.suite, str(acc.served), acc.states) == (
        'nvidia',
        '20260922a',
        [],
    )
    del w.store.objects[ACC + 'InRelease']
    rows = cells(
        w.store,
        FLAT,
        tmp_path / 'manifests',
        'bkt',
        datetime.now(timezone.utc),
    )
    assert 'drift' in next(c for c in rows if c.name == 'acc').states


def test_gc_index_pass(tmp_path):
    "Objects of a snapshot nobody serves any more are dead; live ones kept"
    w = Flat(tmp_path, by_hash=True)
    week = timedelta(days=7)
    assert index_pass(w.store, {'nvidia': FLAT}, ACC, week).dead == {}
    old = {k for k in w.store.objects if k.startswith(ACC + 'by-hash/')}
    w.publish('2.0')
    w.cut_and_apply(today=DAY + timedelta(days=1))
    now = datetime.now(timezone.utc)
    assert index_pass(w.store, {'nvidia': FLAT}, ACC, week, now).dead == {}
    later = now + timedelta(days=30)
    dead = set(index_pass(w.store, {'nvidia': FLAT}, ACC, week, later).dead)
    assert dead == old
    live = {k for k in w.store.objects if k.startswith(ACC)} - old
    assert ACC + 'InRelease' in live


def test_promote_checks_acc_at_the_prefix_root(tmp_path):
    "acc must serve the ref at its top before it can reach prod"
    w = Flat(tmp_path)
    promotion = prepare(
        tmp_path / 'manifests', 'bkt', FLAT, None, None, w.store
    )
    assert promotion.changes == {'nvidia': (None, Ref(None, '20260922a'))}
    promotion.prod.save()
    run(build(promotion.prod, FLAT, w.store), w.store)
    for name in ('InRelease', 'Packages.gz'):
        assert (
            w.store.objects['nvidia/ch/prod/' + name]
            == (w.store.objects[ACC + name])
        )
    w.publish('2.0')
    w.cut_and_apply(today=DAY + timedelta(days=1))
    del w.store.objects[ACC + 'InRelease']
    with pytest.raises(Exception, match='apply acc first'):
        prepare(tmp_path / 'manifests', 'bkt', FLAT, None, None, w.store)


def _config(tmp_path, body: str) -> Config:
    path = tmp_path / 'aptberg.yaml'
    path.write_text(f'scratch: scratch\nbucket: bkt\nupstreams:\n{body}')
    return load(path)


def test_config(tmp_path):
    "flat needs only a keyring and a url; codename names its family"
    cfg = _config(
        tmp_path,
        f"""
  nvidia:
    flat: yes
    codename: noble
    url: http://up.example/cuda/
    keyring: {signer().keyring}
""",
    )
    up = cfg.upstreams['nvidia']
    assert (up.flat, up.codename, up.suites) == (True, 'noble', ('nvidia',))
    assert up.url == 'http://up.example/cuda'


@pytest.mark.parametrize(
    'extra, message',
    [
        ('suites: {noble: [main]}', "no \\['suites'\\]"),
        ('architectures: [amd64]', "no \\['architectures'\\]"),
        ('path: ubuntu', 'also served by'),
    ],
)
def test_config_refuses(tmp_path, extra, message):
    "A flat repository has no suites or architectures, and is alone"
    with pytest.raises(ConfigError, match=message):
        _config(
            tmp_path,
            f"""
  ubuntu:
    url: http://up.example/ubuntu
    keyring: {signer().keyring}
    suites: {{noble: [main]}}
    architectures: [amd64]
  nvidia:
    flat: yes
    url: http://up.example/cuda
    keyring: {signer().keyring}
    {extra}
""",
        )


def test_config_codename_is_for_flat(tmp_path):
    "An ordinary upstream's Release names its own codename"
    with pytest.raises(ConfigError, match='codename is for flat'):
        _config(
            tmp_path,
            f"""
  ubuntu:
    url: http://up.example/ubuntu
    keyring: {signer().keyring}
    suites: {{noble: [main]}}
    architectures: [amd64]
    codename: noble
""",
        )
