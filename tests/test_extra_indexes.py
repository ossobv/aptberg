import gzip
import lzma
from datetime import datetime, timezone

from aptberg.apply import run
from aptberg.history import events
from aptberg.index import selected
from aptberg.manifest import Manifest
from aptberg.plan import build
from aptberg.release import Entry, Release
from aptberg.snapshot import record
from aptberg.status import cells
from aptberg.verify import verify_prefix

from .helpers import UBUNTU, World

EXTRA = {
    'main/dep11/Components-amd64.yml.gz': gzip.compress(b'amd64 components'),
    'main/dep11/Components-arm64.yml.gz': gzip.compress(b'arm64 components'),
    'main/dep11/icons-48x48.tar.gz': gzip.compress(b'icons'),
    'main/dep11/icons-48x48.tar': b'icons uncompressed',
    'main/cnf/Commands-amd64.xz': lzma.compress(b'commands'),
    'main/cnf/Commands-arm64.xz': lzma.compress(b'arm commands'),
}
ACC = 'ubuntu/ch/acc/'


def test_selected():
    "Configured architectures and arch-independent files, compressed"
    rel = Release(
        'noble',
        'noble',
        '',
        ('main',),
        ('amd64', 'arm64'),
        True,
        {
            p: Entry(p, '0' * 64, 1)
            for p in [*EXTRA, 'main/binary-amd64/Packages.xz']
        },
    )
    paths = [e.path for e in selected(rel, ['main'], ['amd64'])]
    assert paths == [
        'main/binary-amd64/Packages.xz',
        'main/cnf/Commands-amd64.xz',
        'main/dep11/Components-amd64.yml.gz',
        'main/dep11/icons-48x48.tar.gz',
    ]


def _acc(w) -> Manifest:
    return Manifest.load(
        w.tmp_path / 'manifests', 'bkt', 'ubuntu', 'ubuntu', None, 'acc'
    )


def test_extras_served(tmp_path):
    "dep11 and cnf of the configured architectures are cut and applied"
    w = World(tmp_path, extra=EXTRA)
    w.sync_pool()
    result = w.cut(w.suite('noble'))
    record(result, tmp_path / 'manifests', 'bkt')
    run(build(_acc(w), UBUNTU, w.store), w.store)
    served = {k for k in w.store.objects if k.startswith(ACC)}
    assert ACC + 'dists/noble/main/dep11/Components-amd64.yml.gz' in served
    assert ACC + 'dists/noble/main/cnf/Commands-amd64.xz' in served
    assert not any('arm64' in k for k in served)

    assert events(w.store, ACC)[-1].suites['noble'].ref == result.ref
    (cell,) = [
        c
        for c in cells(
            w.store,
            UBUNTU,
            tmp_path / 'manifests',
            'bkt',
            datetime.now(timezone.utc),
        )
        if c.name == 'acc'
    ]
    assert (cell.served, cell.states) == (result.ref, [])
    report = verify_prefix(w.store, {'ubuntu': UBUNTU}, ACC)[0]
    assert report.errors == []
