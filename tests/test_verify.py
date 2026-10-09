import re

import httpx

from aptberg import cli
from aptberg.apply import run
from aptberg.history import Served
from aptberg.history import record as record_event
from aptberg.manifest import Manifest, Ref
from aptberg.plan import build
from aptberg.snapshot import record
from aptberg.verify import verify_http, verify_prefix

from .helpers import (
    UBUNTU,
    UP,
    World,
    group_world,
    other_signer,
    signer,
    source_stanza,
)

ACC = 'ubuntu/ch/acc/'
BYHASH = re.compile(r'/by-hash/')
REWRITE = re.compile(r'^([^/]+/ch/[^/]+/)pool/(.*)$')


def _noble(w, **kw):
    reports = {
        r.suite: r for r in verify_prefix(w.store, {'ubuntu': UP}, ACC, **kw)
    }
    return reports['noble']


def test_clean(tmp_path):
    "A freshly applied prefix verifies, shallow and deep"
    w = group_world(tmp_path)
    for deep in (False, True):
        report = _noble(w, deep=deep)
        assert report.errors == [] and report.warnings == []
        assert report.signatures == ['InRelease', 'Release', 'Release.gpg']
        assert report.indexes == 8  # 4 canonical + 4 by-hash
        assert report.pool == 2  # app and libfoo
        assert report.ref == '20260922a'


def test_missing_and_wrong_indexes(tmp_path):
    "A lost by-hash object; swapped content is caught by its ETag"
    w = group_world(tmp_path)
    byhash = next(
        k
        for k in w.store.objects
        if k.startswith(ACC + 'dists/noble/') and BYHASH.search(k)
    )
    del w.store.objects[byhash]
    canonical = ACC + 'dists/noble/main/i18n/Translation-en.xz'
    data, sha256 = w.store.objects[canonical]
    w.store.objects[canonical] = (b'x' * len(data), sha256)
    shallow = _noble(w)
    assert shallow.errors == [
        f'{byhash}: missing',
        f'{canonical}: ETag is not the MD5 the Release names',
    ]
    deep = _noble(w, deep=True)
    assert f'{canonical}: sha256 is not the one the Release names' in (
        deep.errors
    )


def test_pool_content_by_etag(tmp_path):
    "A .deb whose bytes changed is caught from the listing alone"
    w = group_world(tmp_path)
    app = '_pool/_shared/main/a/app/app_1.0_amd64.deb'
    data, sha256 = w.store.objects[app]
    w.store.objects[app] = (bytes(reversed(data)), sha256)
    assert _noble(w).errors == [f'{app}: ETag is not the MD5 Packages names']


def test_source_files_checked_by_etag(tmp_path):
    "Sources' Files: MD5s let source files skip the per-object HEAD"
    w = World(tmp_path, sources=[source_stanza('app-src', 'app')])
    w.sync_pool()
    record(w.cut(w.suite('noble')), tmp_path / 'manifests', 'bkt')
    acc = Manifest.load(
        tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )
    run(build(acc, UBUNTU, w.store), w.store)
    (report,) = verify_prefix(w.store, {'ubuntu': UBUNTU}, ACC)
    assert report.errors == [] and report.fallback == 0
    assert report.pool == 4  # app, libfoo, the .dsc and the tarball


def test_multipart_falls_back(tmp_path):
    "Multipart objects are checked by recorded sha256, and counted"
    w = group_world(tmp_path)
    app = '_pool/_shared/main/a/app/app_1.0_amd64.deb'
    index = ACC + 'dists/noble/main/binary-amd64/Packages.xz'
    w.store.multipart |= {app, index}
    report = _noble(w)
    assert report.errors == [] and report.fallback == 2
    assert report.warnings[0].startswith(
        '2 objects checked by recorded sha256 only'
    )
    assert (report.indexes, report.pool) == (8, 2)
    data, _ = w.store.objects[app]
    w.store.objects[app] = (data, '0' * 64)
    w.store.multipart.add(app)
    assert _noble(w).errors == [
        f'{app}: recorded sha256 is not the one Packages names'
    ]


def test_pool_problems(tmp_path):
    "A missing .deb, and one whose recorded sha256 is wrong (deep)"
    w = group_world(tmp_path)
    app = '_pool/_shared/main/a/app/app_1.0_amd64.deb'
    libfoo = '_pool/_shared/main/l/libfoo/libfoo_1.0_amd64.deb'
    del w.store.objects[app]
    data, _ = w.store.objects[libfoo]
    w.store.objects[libfoo] = (data, '0' * 64)
    # The bytes are right (ETag), only the recorded sha256 is off: deep.
    assert _noble(w).errors == [f'{app}: missing from the pool']
    assert f'{libfoo}: recorded sha256 is not the one Packages names' in (
        _noble(w, deep=True).errors
    )


def test_signature_and_history(tmp_path):
    "A foreign signature; a served Release history does not know"
    w = group_world(tmp_path)
    key = ACC + 'dists/noble/InRelease'
    payload = w.store.objects[ACC + 'dists/noble/Release'][0].decode()
    good = w.store.objects[key]
    w.store.objects[key] = (other_signer().inline(payload), '')
    assert _noble(w).errors[0].startswith('signature: ')
    w.store.objects[key] = good
    record_event(w.store, ACC, {'noble': Served(Ref(None, '20260922a'), '0')})
    assert _noble(w).errors == [
        'served Release is not the one history recorded'
    ]


def _front_end(w, rewrite=True) -> httpx.Client:
    "The bucket served over HTTP, with (or without) the pool rewrite"

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.lstrip('/')
        if rewrite:
            key = REWRITE.sub(r'_pool/_shared/\2', key)
        if key not in w.store.objects:
            return httpx.Response(404)
        data = w.store.objects[key][0]
        if request.method == 'HEAD':
            return httpx.Response(
                200, headers={'content-length': str(len(data))}
            )
        return httpx.Response(200, content=data)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http(tmp_path):
    "Over HTTP: signatures, by-hash indexes, pool files via the rewrite"
    w = group_world(tmp_path)
    reports = verify_http(
        _front_end(w), 'http://aptberg.test', {'ubuntu': UP}, ACC, w.store
    )
    noble = next(r for r in reports if r.suite == 'noble')
    assert noble.errors == []
    assert (noble.indexes, noble.pool) == (1, 2)


def test_http_without_rewrite(tmp_path):
    "A front end without the pool rewrite fails every pool request"
    w = group_world(tmp_path)
    reports = verify_http(
        _front_end(w, rewrite=False),
        'http://aptberg.test',
        {'ubuntu': UP},
        ACC,
        w.store,
    )
    noble = next(r for r in reports if r.suite == 'noble')
    assert len(noble.errors) == 2
    assert all(
        e.endswith('HTTP 404') and '/ch/acc/pool/' in e for e in noble.errors
    )


def test_cli(tmp_path, monkeypatch, capsys, caplog):
    "Every prefix by default, saying so as it goes; exit 1 on errors"
    w = group_world(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
        f'upstreams:\n'
        f'  ubuntu:\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble: [main]\n      noble-updates: [main]\n'
        f'    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    w.store.listed.clear()
    assert cli.main(['-c', str(config), 'verify']) == 0
    # the upstream's own pool, once for every prefix, never all of _pool/
    assert [c for c in w.store.listed if '_pool/' in c] == [
        'etags:_pool/_shared/'
    ]
    assert 'ubuntu/ch/acc/: verifying (1/1)' in caplog.text
    assert 'ubuntu/ch/acc/noble: verifying' in caplog.text
    out = capsys.readouterr().out
    assert 'ubuntu/ch/acc\n' in out
    assert (
        'noble 20260922a: signed (InRelease, Release, Release.gpg), '
        '8 index files, 2 pool files; ok' in out
    )
    del w.store.objects['_pool/_shared/main/a/app/app_1.0_amd64.deb']
    assert cli.main(['-c', str(config), 'verify', 'ubuntu/acc']) == 1
    assert 'ERROR _pool/_shared/main/a/app/app_1.0_amd64.deb: missing' in (
        capsys.readouterr().out
    )
