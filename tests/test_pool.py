from concurrent.futures import ThreadPoolExecutor

import pytest

from aptberg.config import Channel, Filter, Upstream
from aptberg.fetch import fetch_suite
from aptberg.lock import FETCH
from aptberg.pool import PoolConflict, prefixes, select, storage_key

from .helpers import Archive, FakeStore, signer, source_stanza, stanza, sync


def _up(
    name,
    url=None,
    filter_=None,
    suites=('noble',),
    channels=None,
    deb_src=True,
    group=None,
    pool='_shared',
):
    return Upstream(
        name=name,
        keyring=signer().keyring,
        components={s: ('main',) for s in suites},
        architectures=('amd64',),
        url=url,
        channels=channels or {},
        filter=filter_,
        deb_src=deb_src,
        group=group,
        pool=pool,
    )


def test_prefixes_are_per_unit_unless_shared():
    "One _pool/<group or name>/ per unit; pool: names another"
    a = _up('a', pool=None)
    b = _up('b', pool=None, group='g')
    c = _up('c', pool=None, group='g')
    assert prefixes([a, b, c]) == ['_pool/a/', '_pool/g/']
    assert prefixes([b]) == ['_pool/g/']
    assert prefixes([a, _up('s', pool='_shared')]) == [
        '_pool/_shared/',
        '_pool/a/',
    ]
    assert prefixes([a, _up('o', pool='a')]) == ['_pool/a/']


def _fetch_all(archive, tmp_path, *upstreams):
    http = archive.client()
    return [
        fetch_suite(http, up, source, suite, tmp_path)
        for up in upstreams
        for source in up.sources()
        for suite in up.suites
    ]


def _world(tmp_path, include=('app',)):
    """ubuntu noble + noble-updates (filtered), security, and an
    unfiltered anydist kubernetes whose packages need an ubuntu lib"""
    archive = Archive()
    archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [
                stanza('app', Depends='libfoo'),
                stanza('libfoo'),
                stanza('libk8s'),
                stanza('unwanted'),
                stanza('bigthing'),
            ]
        },
    )
    archive.add_suite(
        '/ubuntu',
        'noble-updates',
        {
            'main/binary-amd64': [
                stanza('libfoo', '1.1', Depends='libnew'),
            ]
        },
        codename='noble',
    )
    archive.add_suite(
        '/ubuntu',
        'noble-security',
        {
            'main/binary-amd64': [
                stanza('libfoo', '1.1', Depends='libnew'),
                stanza('libnew'),
            ]
        },
        codename='noble',
    )
    archive.add_suite(
        '/k8s-1.31',
        'anydist',
        {
            'main/binary-amd64': [
                stanza('kubelet', '1.31.1', Depends='libk8s'),
                stanza('kubelet', '1.31.2', Depends='libk8s'),
            ]
        },
    )
    filt = Filter(tuple(include))
    suites = _fetch_all(
        archive,
        tmp_path,
        _up(
            'ubuntu',
            'http://up.example/ubuntu',
            filt,
            suites=('noble', 'noble-updates'),
        ),
        _up(
            'ubuntu-security',
            'http://up.example/ubuntu',
            filt,
            suites=('noble-security',),
        ),
        _up(
            'kubernetes',
            suites=('anydist',),
            channels={'stable': Channel('1.31', 'http://up.example/k8s-1.31')},
        ),
    )
    return archive, suites


def _names(selection):
    return sorted(
        i.filename.rsplit('/', 1)[1] for i in selection.items.values()
    )


def test_select_family(tmp_path):
    """The closure spans suites and upstreams of a codename; unfiltered
    anydist packages are mirrored whole and seed the closure"""
    _, suites = _world(tmp_path)
    sel = select(suites)
    assert _names(sel) == [
        'app_1.0_amd64.deb',
        'kubelet_1.31.1_amd64.deb',
        'kubelet_1.31.2_amd64.deb',
        'libfoo_1.0_amd64.deb',
        'libfoo_1.1_amd64.deb',
        'libk8s_1.0_amd64.deb',
        'libnew_1.0_amd64.deb',
    ]
    assert set(sel.closures) == {'noble'}
    assert sel.closures['noble'].missing_hard() == set()
    item = sel.items['_pool/_shared/main/l/libnew/libnew_1.0_amd64.deb']
    assert item.key == '_pool/_shared/main/l/libnew/libnew_1.0_amd64.deb'
    assert item.url == (
        'http://up.example/ubuntu/pool/main/l/libnew/libnew_1.0_amd64.deb'
    )


def test_restrict(tmp_path):
    "Naming upstreams limits the pool, not the closure"
    _, suites = _world(tmp_path)
    sel = select(suites).restrict(['kubernetes'], suites)
    assert _names(sel) == [
        'kubelet_1.31.1_amd64.deb',
        'kubelet_1.31.2_amd64.deb',
    ]


def test_conflict(tmp_path):
    "One pool path with two contents is fatal, within one pool"
    archive = Archive()
    archive.add_suite(
        '/a', 'noble', {'main/binary-amd64': [stanza('x', content=b'one')]}
    )
    archive.add_suite(
        '/b', 'noble', {'main/binary-amd64': [stanza('x', content=b'two')]}
    )
    suites = _fetch_all(
        archive,
        tmp_path,
        _up('a', 'http://up.example/a'),
        _up('b', 'http://up.example/b'),
    )
    with pytest.raises(PoolConflict, match='pool/main/x/x/x_1.0'):
        select(suites)


def test_no_conflict_across_units(tmp_path):
    "Without pool:, two units' same Filename: stay apart, not fatal"
    archive = Archive()
    archive.add_suite(
        '/a', 'noble', {'main/binary-amd64': [stanza('x', content=b'one')]}
    )
    archive.add_suite(
        '/b', 'noble', {'main/binary-amd64': [stanza('x', content=b'two')]}
    )
    suites = _fetch_all(
        archive,
        tmp_path,
        _up('a', 'http://up.example/a', pool=None),
        _up('b', 'http://up.example/b', pool=None),
    )
    sel = select(suites)
    a = sel.items['_pool/a/main/x/x/x_1.0_amd64.deb']
    b = sel.items['_pool/b/main/x/x/x_1.0_amd64.deb']
    assert a.sha256 != b.sha256
    assert a.key == '_pool/a/main/x/x/x_1.0_amd64.deb'
    assert b.key == '_pool/b/main/x/x/x_1.0_amd64.deb'


def test_conflict_within_shared_unit(tmp_path):
    "Two upstreams of one group share a pool without pool:"
    archive = Archive()
    archive.add_suite(
        '/a', 'noble', {'main/binary-amd64': [stanza('x', content=b'one')]}
    )
    archive.add_suite(
        '/b', 'noble', {'main/binary-amd64': [stanza('x', content=b'two')]}
    )
    suites = _fetch_all(
        archive,
        tmp_path,
        _up('a', 'http://up.example/a', pool=None, group='ab'),
        _up('b', 'http://up.example/b', pool=None, group='ab'),
    )
    with pytest.raises(PoolConflict, match='pool/main/x/x/x_1.0'):
        select(suites)


def test_sync(tmp_path):
    "Uploads what is missing, skips what is there, reports failures"
    archive, suites = _world(tmp_path)
    sel = select(suites)
    store = FakeStore()
    gone = sel.items['_pool/_shared/main/l/libk8s/libk8s_1.0_amd64.deb']
    del archive.files['/ubuntu/' + gone.filename]
    clash = sel.items['_pool/_shared/main/a/app/app_1.0_amd64.deb']
    store.objects[clash.key] = (b'something else', '0' * 64)

    dry = sync(sel, store, archive.client(), tmp_path / 'tmp', dry_run=True)
    assert (dry.selected, dry.present, dry.todo) == (7, 0, 6)
    assert len(store.objects) == 1

    res = sync(
        sel,
        store,
        archive.client(),
        tmp_path / 'tmp',
        concurrency=3,
        progress=False,
    )
    assert res.uploaded == 5
    failed = dict(res.failed)
    assert set(failed) == {clash.key, gone.key}
    assert 'refusing to overwrite' in failed[clash.key]
    assert store.objects[clash.key][0] == b'something else'
    item = sel.items['_pool/_shared/main/l/libnew/libnew_1.0_amd64.deb']
    assert store.objects[item.key] == (b'libnew 1.0 amd64', item.sha256)
    assert list((tmp_path / 'tmp').iterdir()) == []

    again = sync(sel, store, archive.client(), tmp_path / 'tmp')
    assert (again.present, again.uploaded) == (5, 0)


def test_sync_refuses_corrupt_download(tmp_path):
    "Bytes not matching the index are never uploaded"
    archive, suites = _world(tmp_path)
    sel = select(suites)
    item = sel.items['_pool/_shared/main/l/libnew/libnew_1.0_amd64.deb']
    archive.files['/ubuntu/' + item.filename] = b'libnew 1.0 amd6X'
    store = FakeStore()
    res = sync(sel, store, archive.client(), tmp_path / 'tmp', progress=False)
    assert item.key not in store.objects
    assert 'sha256' in dict(res.failed)[item.key]


def test_sync_interrupted_cleans_up_the_fetch_lock(tmp_path, monkeypatch):
    "^C propagates; the fetch marker is still removed"
    archive, suites = _world(tmp_path)
    sel = select(suites)
    store = FakeStore()

    def boom(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr('aptberg.pool._transfer', boom)
    with pytest.raises(KeyboardInterrupt):
        sync(
            sel,
            store,
            archive.client(),
            tmp_path / 'tmp',
            concurrency=1,
            progress=False,
        )
    assert not any(k.startswith(FETCH) for k in store.objects)


def test_sync_second_interrupt_does_not_wait(tmp_path, monkeypatch):
    "A second ^C while waiting for in-flight transfers gives up on them"
    archive, suites = _world(tmp_path)
    sel = select(suites)
    store = FakeStore()
    monkeypatch.setattr(
        'aptberg.pool._transfer',
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    waits = []
    real_shutdown = ThreadPoolExecutor.shutdown

    def flaky_shutdown(self, wait=True, cancel_futures=False):
        waits.append(wait)
        if len(waits) == 1:
            raise KeyboardInterrupt
        return real_shutdown(self, wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(ThreadPoolExecutor, 'shutdown', flaky_shutdown)
    with pytest.raises(KeyboardInterrupt):
        sync(
            sel,
            store,
            archive.client(),
            tmp_path / 'tmp',
            concurrency=1,
            progress=False,
        )
    assert waits == [True, False]


def test_storage_key():
    "pool/<path> is stored at _pool/<pool, else group, else name>/<path>"
    shared = _up('ubuntu')
    own = _up('debian', pool=None)
    grouped = _up('debian-security', pool=None, group='debian')
    filename = 'pool/main/o/openssl/o.deb'
    assert (
        storage_key(shared, filename) == '_pool/_shared/main/o/openssl/o.deb'
    )
    assert storage_key(own, filename) == '_pool/debian/main/o/openssl/o.deb'
    assert (
        storage_key(grouped, filename) == '_pool/debian/main/o/openssl/o.deb'
    )


def test_pool_names_the_pool():
    "pool puts an upstream in the named pool, shared or not"
    a = _up('a', pool=None)
    b = _up('b', pool='a')
    filename = 'pool/main/o/o.deb'
    assert storage_key(a, filename) == storage_key(b, filename)
    assert prefixes([a, b]) == ['_pool/a/']


def _excl_world(tmp_path, include=('*',), hard=()):
    archive = Archive()
    archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [
                stanza(
                    'linux-generic',
                    Depends='linux-image-6.8.0-45-generic',
                    Recommends='nvidia-prime',
                ),
                stanza('linux-image-6.8.0-45-generic'),
                stanza('linux-image-6.8.0-31-generic'),
                stanza('linux-source-6.8.0'),
                stanza('vim'),
                stanza('nvidia-prime'),
                stanza('gpu-tool', Depends='libnvidia-gl | libnvidia-gl-open'),
                stanza('libnvidia-gl'),
            ]
        },
    )
    filt = Filter(
        tuple(include),
        ('linux-*[0-9].[0-9]*.[0-9]*-[0-9]*', 'linux-source-*'),
        tuple(hard),
    )
    return _fetch_all(
        archive, tmp_path, _up('ubuntu', 'http://up.example/ubuntu', filt)
    )


def test_select_soft_exclude(tmp_path):
    "Everything but the soft excludes; what is depended on comes back"
    sel = select(_excl_world(tmp_path, include=('*',)))
    assert _names(sel) == [
        'gpu-tool_1.0_amd64.deb',
        'libnvidia-gl_1.0_amd64.deb',
        'linux-generic_1.0_amd64.deb',
        'linux-image-6.8.0-45-generic_1.0_amd64.deb',
        'nvidia-prime_1.0_amd64.deb',
        'vim_1.0_amd64.deb',
    ]
    noble = sel.closures['noble']
    assert noble.excluded == {
        'linux-image-6.8.0-45-generic',
        'linux-image-6.8.0-31-generic',
        'linux-source-6.8.0',
    }
    assert noble.excluded & noble.wanted == {'linux-image-6.8.0-45-generic'}


def test_select_literal_include_beats_soft(tmp_path):
    "A literally included name stays even when a soft exclude matches it"
    sel = select(
        _excl_world(tmp_path, include=['*', 'linux-image-6.8.0-31-generic'])
    )
    assert 'linux-image-6.8.0-31-generic_1.0_amd64.deb' in _names(sel)
    assert 'linux-image-6.8.0-31-generic' not in sel.closures['noble'].excluded


def test_select_include_globs(tmp_path):
    "Include globs select by name; a glob matching nothing is absent"
    sel = select(_excl_world(tmp_path, include=['vi*', 'emacs*']))
    assert _names(sel) == ['vim_1.0_amd64.deb']
    assert sel.closures['noble'].absent == {'emacs*'}


def test_select_hard_exclude(tmp_path):
    "Hard excluded names never come back; what needed them is reported"
    sel = select(
        _excl_world(tmp_path, include=['*', 'nvidia-prime'], hard=['*nvidia*'])
    )
    assert _names(sel) == [
        'gpu-tool_1.0_amd64.deb',
        'linux-generic_1.0_amd64.deb',
        'linux-image-6.8.0-45-generic_1.0_amd64.deb',
        'vim_1.0_amd64.deb',
    ]
    noble = sel.closures['noble']
    assert noble.blocked == {'nvidia-prime', 'libnvidia-gl'}
    assert noble.broken_hard() == {
        'gpu-tool: libnvidia-gl | libnvidia-gl-open'
    }
    assert noble.broken['Recommends'] == {'linux-generic: nvidia-prime'}
    assert noble.missing_hard() == set()


def _source_world(tmp_path, deb_src=True):
    """A filtered ubuntu (app, not unwanted) and an unfiltered kubernetes,
    each with source packages; the security upstream carries none"""
    archive = Archive()
    archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [
                stanza('app', Depends='libfoo'),
                stanza('libfoo'),
                stanza('unwanted'),
            ]
        },
        sources=[
            source_stanza('app-src', 'app'),
            source_stanza('libfoo-src', 'libfoo, libfoo-dbg'),
            source_stanza('unwanted-src', 'unwanted'),
        ],
    )
    archive.add_suite(
        '/k8s-1.31',
        'anydist',
        {
            'main/binary-amd64': [
                stanza('kubelet', '1.31.1'),
            ]
        },
        sources=[source_stanza('kubernetes-src', 'kubelet')],
    )
    filt = Filter(('app',))
    suites = _fetch_all(
        archive,
        tmp_path,
        _up('ubuntu', 'http://up.example/ubuntu', filt, deb_src=deb_src),
        _up(
            'kubernetes',
            suites=('anydist',),
            deb_src=deb_src,
            channels={'stable': Channel('1.31', 'http://up.example/k8s-1.31')},
        ),
    )
    return archive, suites


def test_select_source_filtered(tmp_path):
    "A filtered upstream's sources ride the binary closure, by Binary:"
    _, suites = _source_world(tmp_path)
    names = _names(select(suites))
    assert 'app-src_1.0.dsc' in names
    assert 'app-src_1.0.orig.tar.xz' in names
    assert 'libfoo-src_1.0.dsc' in names  # libfoo is a dependency
    assert 'unwanted-src_1.0.dsc' not in names  # unwanted is not seeded


def test_select_source_unfiltered(tmp_path):
    "An unfiltered upstream's sources are mirrored whole, like its Packages"
    _, suites = _source_world(tmp_path)
    assert 'kubernetes-src_1.0.dsc' in _names(select(suites))


def test_select_source_off(tmp_path):
    "deb_src: false drops source files even where Binary: matches"
    _, suites = _source_world(tmp_path, deb_src=False)
    names = _names(select(suites))
    assert not any(n.endswith('.dsc') for n in names)
    assert 'app_1.0_amd64.deb' in names


def test_restrict_keeps_named_upstreams_source(tmp_path):
    "restrict() keeps a named upstream's source files, not just its .debs"
    _, suites = _source_world(tmp_path)
    sel = select(suites).restrict(['kubernetes'], suites)
    assert _names(sel) == [
        'kubelet_1.31.1_amd64.deb',
        'kubernetes-src_1.0.dsc',
        'kubernetes-src_1.0.orig.tar.xz',
    ]


def test_sync_redoes_multipart(tmp_path):
    "An object uploaded multipart goes up again as one PUT, if it matches"
    archive, suites = _world(tmp_path)
    sel = select(suites)
    store = FakeStore()
    sync(sel, store, archive.client(), tmp_path / 'tmp', progress=False)
    app = sel.items['_pool/_shared/main/a/app/app_1.0_amd64.deb']
    libfoo = sel.items['_pool/_shared/main/l/libfoo/libfoo_1.0_amd64.deb']
    store.multipart |= {app.key, libfoo.key}
    data, _ = store.objects[libfoo.key]
    store.objects[libfoo.key] = (data, '0' * 64)  # recorded otherwise

    dry = sync(sel, store, archive.client(), tmp_path / 'tmp', dry_run=True)
    assert (dry.todo, dry.redo) == (2, 2)
    res = sync(sel, store, archive.client(), tmp_path / 'tmp', progress=False)
    assert res.uploaded == 1
    assert app.key not in store.multipart
    assert store.objects[app.key] == (b'app 1.0 amd64', app.sha256)
    (failed,) = res.failed
    assert failed[0] == libfoo.key and 'refusing to overwrite' in failed[1]
    assert store.objects[libfoo.key][1] == '0' * 64


def test_debian_pockets_join_their_release(tmp_path):
    "bookworm-security depends on bookworm's libraries, not its own index"
    archive = Archive()
    archive.add_suite(
        '/debian',
        'bookworm',
        {
            'main/binary-amd64': [
                stanza('libc6'),
                stanza('app', Depends='libc6'),
            ]
        },
    )
    archive.add_suite(
        '/debian-security',
        'bookworm-security',
        {'main/binary-amd64': [stanza('app', '1.1', Depends='libc6')]},
    )
    filt = Filter(('app',))
    suites = _fetch_all(
        archive,
        tmp_path,
        _up('debian', 'http://up.example/debian', filt, suites=('bookworm',)),
        _up(
            'debian-security',
            'http://up.example/debian-security',
            filt,
            suites=('bookworm-security',),
        ),
    )
    sel = select(suites)
    assert set(sel.closures) == {'bookworm'}
    assert sel.closures['bookworm'].missing_hard() == set()
    assert 'libc6_1.0_amd64.deb' in _names(sel)
