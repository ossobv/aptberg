import hashlib
import json

import pytest

from aptberg.apply import run
from aptberg.fetch import fetch_suite
from aptberg.history import events
from aptberg.lock import LockError, prefix_key
from aptberg.manifest import Manifest, Ref
from aptberg.plan import PHASES, PlanError, build
from aptberg.pool import PREFIX, select
from aptberg.release import SignatureError
from aptberg.snapshot import cut, record

from .helpers import (
    DAY,
    K8S,
    UBUNTU,
    Archive,
    FakeStore,
    World,
    other_signer,
    stanza,
    sync,
)

ACC = 'ubuntu/ch/acc/'
LOCK = '_lock/ubuntu/ch/acc'


def _acc(w: World, **cut_kw) -> Manifest:
    "Cut noble, record it in ubuntu/acc.yaml, return that manifest"
    result = w.cut(w.suite('noble'), **cut_kw)
    manifests = record(result, w.tmp_path / 'manifests', 'bkt')
    return next(m for m in manifests if m.path.stem == 'acc')


def _world(tmp_path, **kw) -> World:
    w = World(tmp_path, **kw)
    w.sync_pool()
    return w


def _served(w: World, prefix: str = ACC) -> dict[str, bytes]:
    return {
        k[len(prefix) :]: v[0]
        for k, v in w.store.objects.items()
        if k.startswith(prefix)
    }


def test_first_apply(tmp_path):
    "An empty prefix gets by-hash, canonical and Release files"
    w = _world(tmp_path)
    manifest = _acc(w)
    plan = build(manifest, UBUNTU, w.store)
    assert [len(plan.by_phase(p)) for p in PHASES] == [4, 4, 3]
    assert plan.pool_checked == 2
    w.store.puts.clear()
    assert run(plan, w.store) == 11
    served = _served(w)
    assert sorted(k for k in served if '/by-hash/' not in k) == [
        'dists/noble/Contents-amd64.gz',
        'dists/noble/InRelease',
        'dists/noble/Release',
        'dists/noble/Release.gpg',
        'dists/noble/main/binary-amd64/Packages.gz',
        'dists/noble/main/binary-amd64/Packages.xz',
        'dists/noble/main/i18n/Translation-en.xz',
    ]
    assert sorted(k for k in served if '/by-hash/' in k) == sorted(
        op.dst[len(ACC) :] for op in plan.by_phase('byhash')
    )
    assert (
        served['dists/noble/InRelease']
        == (w.archive.files['/ubuntu/dists/noble/InRelease'])
    )
    # Lock taken first, released last; Release files after everything,
    # then the history event.
    assert w.store.puts[0] == LOCK
    assert w.store.puts[-1] == '-' + LOCK
    assert [k.rpartition('/')[2] for k in w.store.puts[-5:-2]] == [
        'InRelease',
        'Release',
        'Release.gpg',
    ]
    assert w.store.puts[-2].startswith('_history/ubuntu/ch/acc/')
    assert LOCK not in w.store.objects
    assert build(manifest, UBUNTU, w.store).ops == []


def test_byhash_objects_resolve(tmp_path):
    "Every by-hash object holds exactly the bytes its name promises"
    w = _world(tmp_path)
    run(build(_acc(w), UBUNTU, w.store), w.store)
    byhash = {k: v for k, v in _served(w).items() if '/by-hash/' in k}
    assert len(byhash) == 4
    for key, data in byhash.items():
        assert hashlib.sha256(data).hexdigest() == key.rpartition('/')[2]


def test_byhash_top_level_entry_no_doubled_slash(tmp_path):
    "Contents-<arch> has no directory of its own; its by-hash key is clean"
    up = UBUNTU
    archive = Archive()
    archive.add_suite(
        '/ubuntu', 'noble', {'main/binary-amd64': [stanza('app')]}
    )
    http = archive.client()
    (source,) = up.sources()
    fs = fetch_suite(http, up, source, 'noble', tmp_path / 'scratch')
    store = FakeStore()
    selection = select([fs])
    sync(selection, store, http, tmp_path / 'tmp', progress=False)
    result = cut(fs, selection, store, store.list_sizes(PREFIX), today=DAY)
    manifest = next(
        m
        for m in record(result, tmp_path / 'manifests', 'bkt')
        if m.path.stem == 'acc'
    )
    plan = build(manifest, up, store)
    contents_ops = [
        op for op in plan.by_phase('byhash') if 'Contents-amd64' in op.src
    ]
    assert contents_ops
    for op in contents_ops:
        assert '//' not in op.dst
        assert op.dst.endswith(f'dists/noble/by-hash/SHA256/{op.sha256}')


def test_flip_keeps_old_byhash(tmp_path):
    "A new snapshot adds by-hash keys; the old ones stay for old clients"
    w = _world(tmp_path)
    manifest = _acc(w)
    run(build(manifest, UBUNTU, w.store), w.store)
    before = _served(w)
    w.publish('1.1')
    w.sync_pool()
    manifest = _acc(w)
    assert manifest.suites['noble'] == Ref(None, '20260922b')
    plan = build(manifest, UBUNTU, w.store)
    # Packages changed (2 by-hash, 2 canonical); the translation did not.
    assert [len(plan.by_phase(p)) for p in PHASES] == [2, 2, 3]
    run(plan, w.store)
    after = _served(w)
    for key, data in before.items():
        if '/by-hash/' in key:
            assert after[key] == data
    assert (
        after['dists/noble/InRelease']
        == (w.archive.files['/ubuntu/dists/noble/InRelease'])
    )
    assert after['dists/noble/InRelease'] != before['dists/noble/InRelease']


def test_rollback_is_apply(tmp_path):
    "Pointing the manifest back at the old id restores the old Release"
    w = _world(tmp_path)
    manifest = _acc(w)
    run(build(manifest, UBUNTU, w.store), w.store)
    old = _served(w)['dists/noble/InRelease']
    w.publish('1.1')
    w.sync_pool()
    run(build(_acc(w), UBUNTU, w.store), w.store)
    manifest.suites['noble'] = Ref(None, '20260922a')
    plan = build(manifest, UBUNTU, w.store)
    assert plan.by_phase('byhash') == []
    run(plan, w.store)
    assert _served(w)['dists/noble/InRelease'] == old


def test_failed_byhash_phase_publishes_nothing(tmp_path):
    "A failure before the release phase leaves no Release written"
    w = _world(tmp_path)
    plan = build(_acc(w), UBUNTU, w.store)
    real = w.store.copy

    def flaky(src, dst):
        if '/by-hash/' in dst and 'i18n' in dst:
            raise OSError('boom')
        real(src, dst)

    w.store.copy = flaky
    with pytest.raises(OSError):
        run(plan, w.store)
    served = _served(w)
    assert not any(
        k.endswith(('InRelease', 'Release', 'Release.gpg'))
        and '/binary-' not in k
        for k in served
    )
    assert not any('/by-hash/' not in k and 'Packages' in k for k in served)
    assert LOCK not in w.store.objects
    w.store.copy = real
    run(build(_acc(w), UBUNTU, w.store), w.store)
    assert 'dists/noble/InRelease' in _served(w)


def test_history_failure_does_not_fail_apply(tmp_path, caplog):
    "The prefix flipped all the same; the lost event is logged loudly"
    w = _world(tmp_path)
    plan = build(_acc(w), UBUNTU, w.store)
    real = w.store.put_bytes

    def no_history(key, data, sha256):
        if key.startswith('_history/'):
            raise OSError('boom')
        real(key, data, sha256)

    w.store.put_bytes = no_history
    assert run(plan, w.store, concurrency=1) == 11
    assert 'dists/noble/InRelease' in _served(w)
    assert 'could not record history: boom' in caplog.text
    assert events(w.store, ACC) == []


def test_index_only_apply_is_recorded(tmp_path):
    "An apply that changes no Release still changed what is served"
    w = _world(tmp_path)
    plan = build(_acc(w), UBUNTU, w.store)
    plan.ops = [op for op in plan.ops if op.phase != 'release']
    assert run(plan, w.store) == 8
    assert [list(e.suites) for e in events(w.store, ACC)] == [['noble']]


def test_stale_signature_deleted(tmp_path):
    "A signature file the new snapshot lacks is removed at the flip"
    w = _world(tmp_path)
    run(build(_acc(w), UBUNTU, w.store), w.store)
    del w.archive.files['/ubuntu/dists/noble/InRelease']
    w.publish('1.1', inrelease=False)
    w.sync_pool()
    plan = build(_acc(w), UBUNTU, w.store)
    assert [(op.kind, op.dst) for op in plan.by_phase('release')] == [
        ('copy', ACC + 'dists/noble/Release'),
        ('copy', ACC + 'dists/noble/Release.gpg'),
        ('delete', ACC + 'dists/noble/InRelease'),
    ]
    run(plan, w.store)
    assert 'dists/noble/InRelease' not in _served(w)


def test_pool_must_be_complete(tmp_path):
    "A snapshot whose .debs are not all in _pool/ is refused"
    w = World(tmp_path)
    manifest = _acc(w, allow_missing=True)
    plan = build(manifest, UBUNTU, w.store)
    assert len(plan.pool_accepted_missing) == 2
    marker = '_snap/ubuntu/noble/20260922a/snapshot.json'
    data = json.loads(w.store.objects[marker][0])
    data['missing'] = []
    w.store.objects[marker] = (json.dumps(data).encode(), '')
    with pytest.raises(PlanError, match='not in _pool/'):
        build(manifest, UBUNTU, w.store)


def test_summary(tmp_path):
    "One line for the pool check and one per phase"
    w = World(tmp_path)
    plan = build(_acc(w, allow_missing=True), UBUNTU, w.store)
    lines = plan.summary()
    assert lines[:2] == [
        f'{ACC}: noble 20260922a',
        '  pool     0 files present, 2 missing (accepted at cut)',
    ]
    # Signatures vary in size; the counts do not.
    assert [line.split(',')[0] for line in lines[2:]] == [
        '  byhash   4 copies',
        '  index    4 copies',
        '  release  3 copies',
    ]


def test_empty_manifest(tmp_path):
    "A manifest without suites plans nothing and lists nothing"
    w = _world(tmp_path)
    manifest = _acc(w)
    manifest.suites.clear()
    w.store.objects.clear()
    plan = build(manifest, UBUNTU, w.store)
    assert plan.ops == []


def test_marker_must_match_release(tmp_path):
    "An entry in snapshot.json the signed Release does not vouch for"
    w = _world(tmp_path)
    manifest = _acc(w)
    marker = '_snap/ubuntu/noble/20260922a/snapshot.json'
    data = json.loads(w.store.objects[marker][0])
    data['entries'][0][1] = '0' * 64
    w.store.objects[marker] = (json.dumps(data).encode(), '')
    with pytest.raises(PlanError, match='not as the Release says'):
        build(manifest, UBUNTU, w.store)


def test_marker_must_be_this_snapshots(tmp_path):
    "A snapshot.json made for another Release, or another suite, is refused"
    w = _world(tmp_path)
    manifest = _acc(w)
    marker = '_snap/ubuntu/noble/20260922a/snapshot.json'
    good = w.store.objects[marker][0]
    data = json.loads(good)
    data['release_sha256'] = '0' * 64
    w.store.objects[marker] = (json.dumps(data).encode(), '')
    with pytest.raises(PlanError, match='Release does not match'):
        build(manifest, UBUNTU, w.store)
    data = json.loads(good)
    data['suite'] = 'jammy'
    w.store.objects[marker] = (json.dumps(data).encode(), '')
    with pytest.raises(PlanError, match='is for jammy'):
        build(manifest, UBUNTU, w.store)


def test_signature_file_must_exist(tmp_path):
    "A signature file snapshot.json names but _snap/ lacks is refused"
    w = _world(tmp_path)
    manifest = _acc(w)
    del w.store.objects['_snap/ubuntu/noble/20260922a/dists/noble/InRelease']
    with pytest.raises(PlanError, match='InRelease: missing'):
        build(manifest, UBUNTU, w.store)


def test_signature_verified_again(tmp_path):
    "A snapshot re-signed by someone else is refused at apply"
    w = _world(tmp_path)
    manifest = _acc(w)
    key = '_snap/ubuntu/noble/20260922a/dists/noble/InRelease'
    payload = w.archive.files['/ubuntu/dists/noble/Release'].decode()
    w.store.objects[key] = (other_signer().inline(payload), '')
    with pytest.raises(SignatureError):
        build(manifest, UBUNTU, w.store)


def test_missing_snapshot(tmp_path):
    "A manifest naming a snapshot that does not exist is refused"
    w = _world(tmp_path)
    manifest = _acc(w)
    manifest.suites['noble'] = Ref(None, '20260101a')
    with pytest.raises(PlanError, match='no snapshot.json'):
        build(manifest, UBUNTU, w.store)


def test_ref_must_fit_upstream(tmp_path):
    "Tree refs only for channel upstreams, bare ids only for the others"
    w = _world(tmp_path)
    manifest = _acc(w)
    manifest.suites['noble'] = Ref('1.31', '20260922a')
    with pytest.raises(PlanError, match='has no channels'):
        build(manifest, UBUNTU, w.store)


def test_channel_prefix(tmp_path):
    "A channel upstream's prefix serves the tree its ref names"
    w = _world(tmp_path)
    result = w.cut(w.suite('anydist', '1.31'))
    manifests = record(result, tmp_path / 'manifests', 'bkt')
    stable = next(m for m in manifests if m.path.stem == 'stable-acc')
    run(build(stable, K8S, w.store), w.store)
    served = _served(w, 'kubernetes/ch/stable-acc/')
    assert (
        served['dists/anydist/InRelease']
        == (w.archive.files['/k8s-1.31/dists/anydist/InRelease'])
    )
    again = Manifest.read(stable.path, 'bkt', 'kubernetes')
    assert again.key_prefix == 'kubernetes/ch/stable-acc/'


def test_lock(tmp_path):
    "A held lock, outside the served prefix, stops apply; --force breaks it"
    assert prefix_key(ACC) == LOCK
    w = _world(tmp_path)
    plan = build(_acc(w), UBUNTU, w.store)
    w.store.objects[LOCK] = (b'{"host": "other"}', '')
    with pytest.raises(LockError, match='other'):
        run(plan, w.store)
    assert 'dists/noble/InRelease' not in _served(w)
    run(plan, w.store, force=True)
    assert LOCK not in w.store.objects
