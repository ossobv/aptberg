"""path: two independent upstreams served at one shared location

Cuts across gc, verify and status, which all have to resolve which
upstream owns a given suite instead of assuming the whole prefix
belongs to one -- so this is tested end to end, through a real
fetch/cut/apply, rather than against each module in isolation.
"""

from datetime import datetime, timedelta, timezone

from aptberg.apply import run
from aptberg.config import Config, Upstream
from aptberg.fetch import fetch_suite
from aptberg.gc import index_pass
from aptberg.plan import build
from aptberg.pool import PREFIX, select
from aptberg.snapshot import cut, record
from aptberg.status import cells
from aptberg.verify import verify_prefix

from .helpers import (
    DAY,
    Archive,
    FakeStore,
    other_signer,
    signer,
    stanza,
    sync,
)

WEEK = timedelta(days=7)
PREFIX_ = 'main/ch/acc/'


def _world(tmp_path):
    """ "main" (trixie) and "old" (jessie, path: main), distinct keyrings

    Distinct signers so a verify_prefix that used the wrong upstream's
    keyring for a suite would fail with a signature error, not just
    look up the wrong _snap/ path.
    """
    archive = Archive()
    archive.add_suite(
        '/main',
        'trixie',
        {'main/binary-amd64': [stanza('app')]},
        signer_=signer(),
    )
    archive.add_suite(
        '/old',
        'jessie',
        {'main/binary-amd64': [stanza('oldapp')]},
        signer_=other_signer(),
    )
    main = Upstream(
        name='main',
        keyring=signer().keyring,
        components={'trixie': ('main',)},
        architectures=('amd64',),
        url='http://up.example/main',
        pool='_shared',
    )
    old = Upstream(
        name='old',
        keyring=other_signer().keyring,
        components={'jessie': ('main',)},
        architectures=('amd64',),
        url='http://up.example/old',
        pool='_shared',
        path='main',
    )
    http = archive.client()
    main_fs = fetch_suite(
        http, main, main.sources()[0], 'trixie', tmp_path / 'scratch'
    )
    old_fs = fetch_suite(
        http, old, old.sources()[0], 'jessie', tmp_path / 'scratch'
    )
    store = FakeStore()
    selection = select([main_fs, old_fs])
    sync(selection, store, http, tmp_path / 'tmp', progress=False)
    present = store.list_sizes(PREFIX)
    main_manifests = record(
        cut(main_fs, selection, store, present, today=DAY),
        tmp_path / 'manifests',
        'bkt',
    )
    old_manifests = record(
        cut(old_fs, selection, store, present, today=DAY),
        tmp_path / 'manifests',
        'bkt',
    )
    acc_main = next(m for m in main_manifests if m.path.stem == 'acc')
    acc_old = next(m for m in old_manifests if m.path.stem == 'acc')
    run(build(acc_main, main, store), store)
    run(build(acc_old, old, store), store)
    cfg = Config(
        scratch=tmp_path / 'scratch',
        bucket='bkt',
        upstreams={'main': main, 'old': old},
        manifests=tmp_path / 'manifests',
    )
    return cfg, store


def test_served_upstreams(tmp_path):
    "Both land under main/ch/, discoverable from either name"
    cfg, store = _world(tmp_path)
    assert set(cfg.served_upstreams('main')) == {'main', 'old'}
    assert cfg.served_upstreams('old') == {}
    for suite in ('dists/trixie/InRelease', 'dists/jessie/InRelease'):
        assert PREFIX_ + suite in store.objects


def test_gc_index_pass_protects_both(tmp_path):
    "Neither suite's fresh objects are dead right after applying both"
    cfg, store = _world(tmp_path)
    found = index_pass(store, cfg.served_upstreams('main'), PREFIX_, WEEK)
    assert found.dead == {}


def test_verify_uses_each_suites_own_keyring(tmp_path):
    "A shared prefix verifies both suites against their own upstream"
    cfg, store = _world(tmp_path)
    reports = verify_prefix(store, cfg.served_upstreams('main'), PREFIX_)
    by_suite = {r.suite: r for r in reports}
    assert set(by_suite) == {'trixie', 'jessie'}
    assert by_suite['trixie'].errors == []
    assert by_suite['jessie'].errors == []


def test_status_excludes_the_co_tenants_suite(tmp_path):
    "main's status rows never include old's jessie, and vice versa"
    cfg, store = _world(tmp_path)
    now = datetime.now(timezone.utc)

    def _cells(name):
        upstream = cfg.upstreams[name]
        foreign = frozenset(
            s
            for n, u in cfg.served_upstreams(upstream.served).items()
            if n != name
            for s in u.suites
        )
        return {
            c.suite
            for c in cells(
                store,
                upstream,
                cfg.manifests,
                cfg.bucket,
                now,
                foreign=foreign,
            )
        }

    assert _cells('main') == {'trixie'}
    assert _cells('old') == {'jessie'}
