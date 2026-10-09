"""Cut: a verified suite in scratch becomes an immutable snapshot

    _snap/<upstream>/[<tree>/]<suite>/<id>/dists/<suite>/InRelease ...
    _snap/<upstream>/[<tree>/]<suite>/<id>/dists/<suite>/main/...
    _snap/<upstream>/[<tree>/]<suite>/<id>/filenames.gz
    _snap/<upstream>/[<tree>/]<suite>/<id>/selected.gz
    _snap/<upstream>/[<tree>/]<suite>/<id>/snapshot.json

A flat upstream is stored the same way (see layout.py); only apply puts
its files elsewhere.

Like _pool/ and _lock/, _snap/ is outside the served tree: clients only
ever see <upstream>/ch/<prefix>/.

filenames.gz lists every file the snapshot's Packages and Sources name;
it is what gc keeps in _pool/ while the snapshot exists. selected.gz
lists the ones the filter picked when the snapshot was cut; apply checks
those are in _pool/. snapshot.json is written last and marks the snapshot
complete: a directory without it is an interrupted cut, which nothing
references and the next cut of the same day resumes.

Snapshot ids are YYYYMMDD plus a letter, 26 per day. When the verified
Release equals that of the latest complete snapshot, and the same index
files were taken from it, that id is reused and nothing is written: the
fetch has not moved. Taking more files from an unchanged Release (dep11
added to the config, say) makes a new snapshot.
"""

import gzip
import hashlib
import json
import logging
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from . import index
from .deb822 import stanzas
from .fetch import SIGNATURES, FetchedSuite, file_sha256
from .manifest import AUTO_STAGES, ID, Manifest, Ref
from .pool import PREFIX, Selection, storage_key
from .store import Store

log = logging.getLogger(__name__)

SNAP = '_snap/'
MARKER = 'snapshot.json'
LETTERS = 'abcdefghijklmnopqrstuvwxyz'


class CutError(Exception):
    "A snapshot could not be cut"


def suite_prefix(upstream: str, tree: str | None, suite: str) -> str:
    "The key prefix holding every snapshot id of a suite"
    return f'{SNAP}{upstream}/{tree + "/" if tree else ""}{suite}/'


def snapshot_root(upstream: str, suite: str, ref: Ref) -> str:
    "The key prefix of one snapshot"
    return f'{suite_prefix(upstream, ref.tree, suite)}{ref.id}/'


def base(fs: FetchedSuite) -> str:
    "The key prefix holding every snapshot id of a fetched suite"
    return suite_prefix(fs.upstream.name, fs.source.tree, fs.suite)


def next_id(existing: Iterable[str], today: date) -> str:
    "The first free id of the day"
    stem = today.strftime('%Y%m%d')
    taken = {i for i in existing if i.startswith(stem)}
    for letter in LETTERS:
        if stem + letter not in taken:
            return stem + letter
    raise CutError(f'all 26 snapshot ids of {stem} are taken')


@dataclass
class CutResult:
    "What a cut did for one suite"

    suite: FetchedSuite
    ref: Ref
    reused: bool = False
    uploaded: int = 0
    missing: list[str] = field(default_factory=list)


def pool_files(fs: FetchedSuite) -> set[str]:
    "Every Filename the suite's Packages and Sources indexes name"
    out = set()
    for entry in index.packages_indexes(fs.entries).values():
        with index.open_text(fs.path / entry.path) as fh:
            out.update(s['Filename'] for s in stanzas(fh) if 'Filename' in s)
    for entry in index.sources_indexes(fs.entries).values():
        with index.open_text(fs.path / entry.path) as fh:
            for stanza in stanzas(fh):
                out.update(f for f, _, _ in index.source_files(stanza))
    return out


def cut(
    fs: FetchedSuite,
    selection: Selection,
    store: Store,
    present: dict[str, int],
    today: date | None = None,
    allow_missing: bool = False,
    dry_run: bool = False,
) -> CutResult:
    """Cut one fetched suite into _snap/, or find it already there

    present is a listing of _pool/ (key -> size). Every pool file the
    filter selected for this suite must be in it: a snapshot naming
    .debs we do not have would 404 once applied. allow_missing records
    them in snapshot.json instead of refusing (for files upstream lists
    but no longer serves).
    """
    today = today or datetime.now(timezone.utc).date()
    prefix = base(fs)
    tree = fs.source.tree
    snap_id, reused = _snapshot_id(store, prefix, fs, today)
    if reused:
        log.info('%s%s: unchanged', prefix, snap_id)
        return CutResult(fs, Ref(tree, snap_id), reused=True)
    all_files = pool_files(fs)
    selected = sorted(
        f for f in all_files if storage_key(fs.upstream, f) in selection.items
    )
    missing = [
        f
        for f in selected
        if selection.items[storage_key(fs.upstream, f)].key not in present
    ]
    result = CutResult(fs, Ref(tree, snap_id), missing=missing)
    if missing and not allow_missing:
        raise CutError(
            f'{prefix}{snap_id}: {len(missing)} selected pool files are '
            f'not in {PREFIX}, e.g. {missing[0]}; run fetch first, or '
            f'cut with --allow-missing'
        )
    if dry_run:
        return result
    result.uploaded = _write(
        store, fs, prefix, snap_id, all_files, selected, missing
    )
    log.info('%s%s/: cut, %d files uploaded', prefix, snap_id, result.uploaded)
    return result


def _snapshot_id(
    store: Store, prefix: str, fs: FetchedSuite, today: date
) -> tuple[str, bool]:
    """The id to cut fs under, and whether that snapshot exists already

    The latest complete snapshot if it holds what fs would cut; else
    the newest id of today that has no marker, which is an interrupted
    cut that nothing references and is resumed rather than skipped;
    else the next free id.
    """
    ids = sorted(
        (i for i in store.list_dirs(prefix) if ID.fullmatch(i)), reverse=True
    )
    latest = _latest_complete(store, prefix, ids)
    if latest and _same(latest[1], fs):
        return latest[0], True
    resume = [
        i
        for i in ids[:1]
        if i.startswith(today.strftime('%Y%m%d'))
        and (not latest or i != latest[0])
    ]
    return (resume[0] if resume else next_id(ids, today)), False


def _write(
    store: Store,
    fs: FetchedSuite,
    prefix: str,
    snap_id: str,
    all_files: set[str],
    selected: list[str],
    missing: list[str],
) -> int:
    """Upload a snapshot, its marker last; returns files uploaded

    The marker (snapshot.json) is what makes a snapshot complete: until
    it is there, nothing uses the snapshot and a new cut resumes it.
    """
    root = f'{prefix}{snap_id}/'
    dists = f'{root}dists/{fs.suite}/'
    uploads = [
        (dists + e.path, fs.path / e.path, e.sha256) for e in fs.entries
    ]
    uploaded = _upload_files(store, uploads)
    lists = {
        'filenames.gz': _gzip_lines(sorted(all_files)),
        'selected.gz': _gzip_lines(selected),
    }
    for name, data in lists.items():
        store.put_bytes(root + name, data, _sha256(data))
    # The signature files go last among the dists: a snapshot is never
    # observable anyway, but a partial one should not look signed.
    present = [n for n in SIGNATURES if (fs.path / n).exists()]
    uploaded += _upload_files(
        store,
        [(dists + n, fs.path / n, file_sha256(fs.path / n)) for n in present],
    )
    marker = {
        'upstream': fs.upstream.name,
        'tree': fs.source.tree,
        'suite': fs.suite,
        'id': snap_id,
        'codename': fs.codename,
        'release_date': fs.release.date,
        'release_sha256': fs.release_sha256,
        'source': fs.source.url,
        'cut_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'signatures': present,
        'entries': [[e.path, e.sha256, e.size] for e in fs.entries],
        'lists': {n: _sha256(d) for n, d in lists.items()},
        'missing': missing,
    }
    data = json.dumps(marker, indent=1, sort_keys=True).encode() + b'\n'
    store.put_bytes(root + MARKER, data, _sha256(data))
    return uploaded


def record(
    result: CutResult,
    root: Path,
    bucket: str,
    channels: Iterable[str] | None = None,
    stages: Iterable[str] | None = None,
) -> list[Manifest]:
    """Point the cur and acc manifests following the cut tree at the new ref

    For an upstream with channels that is every <channel>-{cur,acc} whose
    channel follows the cut tree (restricted to channels when given);
    for one without, cur and acc. Both are AUTO_STAGES: cut keeps them
    current on every run, no promotion involved -- unlike prod, which
    only moves via promote. stages restricts which of them are touched
    (e.g. just "cur", so a cron run keeping cur current never writes or
    applies acc); default is both.
    """
    up = result.suite.upstream
    if up.channels:
        names = [
            c
            for c, ch in sorted(up.channels.items())
            if ch.tree == result.ref.tree
            and (channels is None or c in channels)
        ]
    else:
        names = [None]
    wanted = [s for s in AUTO_STAGES if stages is None or s in stages]
    out = []
    for channel in names:
        for stage in wanted:
            manifest = Manifest.load(
                root, bucket, up.name, up.served, channel, stage
            )
            if manifest.suites.get(result.suite.suite) != result.ref:
                manifest.suites[result.suite.suite] = result.ref
                manifest.save()
            out.append(manifest)
    return out


def _same(marker: dict, fs: FetchedSuite) -> bool:
    "Whether a snapshot holds what fs would cut: same Release, same files"
    return marker.get('release_sha256') == fs.release_sha256 and sorted(
        p for p, _, _ in marker['entries']
    ) == sorted(e.path for e in fs.entries)


def _latest_complete(
    store: Store, prefix: str, ids: list[str]
) -> tuple[str, dict] | None:
    for snap_id in ids:
        raw = store.get_bytes(f'{prefix}{snap_id}/{MARKER}')
        if raw is not None:
            return snap_id, json.loads(raw)
    return None


def _upload_files(store: Store, files: list, workers: int = 8) -> int:
    "Upload (key, path, sha256) triples not already there; return count"

    def one(job) -> int:
        key, path, sha256 = job
        if store.sha256(key) == sha256:
            return 0
        store.put_file(key, path, sha256)
        return 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(one, files))


def _gzip_lines(lines: list[str]) -> bytes:
    data = ''.join(line + '\n' for line in lines).encode()
    return gzip.compress(data, mtime=0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
