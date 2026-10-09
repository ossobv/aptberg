import json
from dataclasses import replace

import pytest

from aptberg.apply import run
from aptberg.fetch import fetch_suite
from aptberg.manifest import Manifest, Ref
from aptberg.plan import build
from aptberg.pool import select
from aptberg.promote import (
    PromoteError,
    codename_suites,
    prepare,
    prepare_group,
    units,
)
from aptberg.snapshot import record

from .helpers import K8S, SEC, UP, UPG, World, group_world, stanza, two_suites


def _manifest(w, stage, channel=None, upstream='ubuntu') -> Manifest:
    return Manifest.load(
        w.tmp_path / 'manifests', 'bkt', upstream, upstream, channel, stage
    )


def _apply(w, manifest, upstream=UP):
    run(build(manifest, upstream, w.store), w.store)


def _prepare(w, upstream=UP, channel=None, suites=None):
    return prepare(
        w.tmp_path / 'manifests', 'bkt', upstream, channel, suites, w.store
    )


def test_refuses_what_acc_does_not_serve(tmp_path):
    "A cut recorded in acc but never applied cannot reach prod"
    w = two_suites(tmp_path)
    with pytest.raises(PromoteError, match='apply acc first'):
        _prepare(w)


def test_promote_all(tmp_path):
    "Every acc suite moves to prod; prod then serves what acc serves"
    w = two_suites(tmp_path)
    _apply(w, _manifest(w, 'acc'))
    promotion = _prepare(w)
    assert promotion.changes == {
        'noble': (None, Ref(None, '20260922a')),
        'noble-updates': (None, Ref(None, '20260922a')),
    }
    assert not promotion.prod.path.exists()  # nothing written yet
    promotion.prod.save()
    _apply(w, promotion.prod)
    for suite in ('noble', 'noble-updates'):
        key = f'dists/{suite}/InRelease'
        assert (
            w.store.objects['ubuntu/ch/prod/' + key]
            == (w.store.objects['ubuntu/ch/acc/' + key])
        )
    assert _prepare(w).changes == {}


def test_promote_one_suite(tmp_path):
    "--suite moves only that suite; the others keep their prod ref"
    w = two_suites(tmp_path)
    _apply(w, _manifest(w, 'acc'))
    promotion = _prepare(w, suites=['noble-updates'])
    assert list(promotion.changes) == ['noble-updates']
    assert promotion.prod.suites == {'noble-updates': Ref(None, '20260922a')}
    with pytest.raises(PromoteError, match='no suite jammy'):
        _prepare(w, suites=['jammy'])


def test_acc_moved_on_without_apply(tmp_path):
    "Once acc's manifest is ahead of what it serves, promote refuses"
    w = two_suites(tmp_path)
    _apply(w, _manifest(w, 'acc'))
    w.archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [
                stanza('app', '2.0', Depends='libfoo'),
                stanza('libfoo'),
            ]
        },
    )
    w.suites[0] = fetch_suite(
        w.archive.client(), UP, UP.sources()[0], 'noble', tmp_path / 'scratch'
    )
    w.selection = select(w.suites)
    w.sync_pool()
    record(w.cut(w.suite('noble')), tmp_path / 'manifests', 'bkt')
    assert _manifest(w, 'acc').suites['noble'] == Ref(None, '20260922b')
    with pytest.raises(PromoteError, match='does not serve noble'):
        _prepare(w)
    # The other suite alone is still fine.
    assert list(_prepare(w, suites=['noble-updates']).changes) == [
        'noble-updates'
    ]


def test_channels(tmp_path):
    "Channel upstreams promote one channel; others are untouched"
    w = two_suites(tmp_path)
    for tree in ('1.30', '1.31'):
        record(w.cut(w.suite('anydist', tree)), tmp_path / 'manifests', 'bkt')
    stable = _manifest(w, 'acc', 'stable', 'kubernetes')
    _apply(w, stable, K8S)
    with pytest.raises(PromoteError, match='name one of'):
        _prepare(w, K8S)
    with pytest.raises(PromoteError, match='no channel'):
        _prepare(w, K8S, 'nightly')
    with pytest.raises(PromoteError, match='has no channels'):
        _prepare(w, UP, 'stable')
    promotion = _prepare(w, K8S, 'stable')
    assert promotion.prod.path.name == 'stable-prod.yaml'
    assert promotion.changes == {'anydist': (None, Ref('1.31', '20260922a'))}
    # experimental follows the same tree but was never applied.
    with pytest.raises(PromoteError, match='apply acc first'):
        _prepare(w, K8S, 'experimental')


def test_missing_acc_manifest(tmp_path):
    "No acc manifest means nothing was cut for that prefix"
    w = World(tmp_path)
    with pytest.raises(PromoteError, match='nothing was cut'):
        _prepare(w)


def _group(w, suites=None, channel=None, upstreams=(UPG, SEC)):
    return prepare_group(
        w.tmp_path / 'manifests',
        'bkt',
        list(upstreams),
        channel,
        suites,
        w.store,
    )


def test_group_all(tmp_path):
    "Without --suite every member promotes every acc suite"
    w = group_world(tmp_path)
    got = {p.prod.path.parent.name: sorted(p.changes) for p in _group(w)}
    assert got == {
        'ubuntu': ['noble', 'noble-updates'],
        'ubuntu-security': ['noble-security'],
    }


def test_group_suites_go_where_they_live(tmp_path):
    "Each member takes the listed suites it has; others are left out"
    w = group_world(tmp_path)
    (only,) = _group(w, ['noble-security'])
    assert only.prod.path.parent.name == 'ubuntu-security'
    both = _group(w, ['noble', 'noble-security'])
    assert [sorted(p.changes) for p in both] == [['noble'], ['noble-security']]
    with pytest.raises(PromoteError, match='has suite jammy-security'):
        _group(w, ['noble', 'jammy-security'])


def test_group_all_or_nothing(tmp_path):
    "One member acc does not serve yet: nothing is promoted"
    w = group_world(tmp_path, apply_security=False)
    with pytest.raises(PromoteError, match='apply acc first'):
        _group(w)
    assert not (tmp_path / 'manifests' / 'ubuntu' / 'prod.yaml').exists()


def test_group_channel_mismatch(tmp_path):
    "A group mixing channel and plain upstreams says which one objects"
    w = group_world(tmp_path)
    with pytest.raises(PromoteError, match='kubernetes has channels'):
        _group(w, upstreams=(UPG, K8S))
    with pytest.raises(PromoteError, match='ubuntu has no channels'):
        _group(w, channel='stable', upstreams=(UPG, K8S))


def test_codename_suites(tmp_path):
    "Codenames come from the snapshots, across the group"
    w = group_world(tmp_path)
    root = tmp_path / 'manifests'
    assert codename_suites(
        root, 'bkt', [UPG, SEC], None, ['noble'], w.store
    ) == ['noble', 'noble-security', 'noble-updates']
    key = '_snap/ubuntu/noble-updates/20260922a/snapshot.json'
    marker = json.loads(w.store.objects[key][0])
    marker['codename'] = 'other'
    w.store.objects[key] = (json.dumps(marker).encode(), '')
    assert codename_suites(
        root, 'bkt', [UPG, SEC], None, ['noble'], w.store
    ) == ['noble', 'noble-security']
    assert codename_suites(
        root, 'bkt', [UPG, SEC], None, ['other'], w.store
    ) == ['noble-updates']
    with pytest.raises(PromoteError, match='has codename jammy'):
        codename_suites(root, 'bkt', [UPG, SEC], None, ['jammy'], w.store)
    with pytest.raises(PromoteError, match='kubernetes has channels'):
        codename_suites(root, 'bkt', [UPG, K8S], None, ['noble'], w.store)


def test_units():
    "A group's plain members go together, its channel members per channel"
    k8s = replace(K8S, group='ubuntu')
    got = [(str(u), u.channel) for u in units([UPG, SEC, k8s])]
    assert got == [
        ('ubuntu, ubuntu-security', None),
        ('kubernetes verystable', 'verystable'),
        ('kubernetes stable', 'stable'),
        ('kubernetes experimental', 'experimental'),
    ]
    assert [str(u) for u in units([UPG, k8s], ['stable'])] == [
        'kubernetes stable'
    ]
    with pytest.raises(PromoteError, match='no channel nightly'):
        units([UPG, k8s], ['nightly'])
