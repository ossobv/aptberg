"""Verify: re-check a published channel prefix as a client would

From the bucket (verify_prefix), per suite the prefix serves:

- every signature form published verifies against the upstream keyring,
  and the forms agree
- every index we mirror is at its canonical path and, if the Release
  advertises by-hash, at its by-hash path, with the size and MD5 the
  signed Release names (deep: downloaded and SHA256-hashed instead)
- the served Release is the one _history/ recorded
- every pool file the served snapshot selected is in _pool/ with the
  size and MD5 its Packages entry (or its Sources stanza's Files:)
  names (deep: and the recorded sha256)

The MD5s cost nothing: aptberg uploads with single PUTs, so each ETag in
the listing is the content's MD5 as the server computed it. An object
uploaded multipart (ETag ending in -N), or an entry without an MD5, falls
back to the sha256 aptberg recorded at upload, and is counted in a
warning: a fetch uploads such pool objects again.

Over HTTP (verify_http), what apt would get from the front end: the
Release files, each Packages and Sources index (by-hash where the
Release advertises it), and a sample of pool files through
<prefix>/pool/, which exercises the pool rewrite.
"""

import gzip
import hashlib
import json
import logging
import random
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from . import index
from .config import Upstream
from .deb822 import stanzas
from .fetch import SIGNATURES
from .history import events, state_at
from .layout import dists as served_dists
from .layout import listing_root, owner, served_suites
from .manifest import Ref
from .pool import list_etags as list_pool_etags
from .pool import served_name, storage_key
from .progress import Progress
from .release import Entry, Release, SignatureError, verified_release
from .snapshot import MARKER, snapshot_root
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class SuiteReport:
    "What was checked for one suite, and what was wrong"

    suite: str
    ref: str = ''
    signatures: list[str] = field(default_factory=list)
    indexes: int = 0
    pool: int = 0
    fallback: int = 0  # checked by recorded sha256, not by ETag
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        "One summary line"
        what = [
            f'signed ({", ".join(self.signatures)})'
            if self.signatures
            else 'unsigned',
            f'{self.indexes} index files',
            f'{self.pool} pool files',
        ]
        head = f'{self.suite} {self.ref or "(no history)"}: '
        return (
            head
            + ', '.join(what)
            + (f'; {len(self.errors)} errors' if self.errors else '; ok')
        )


def verify_prefix(
    store: Store,
    upstreams: dict[str, Upstream],
    prefix: str,
    deep: bool = False,
    concurrency: int = 8,
    pool: dict[str, tuple[int, str]] | None = None,
) -> list[SuiteReport]:
    """Every suite a channel prefix serves, checked from the bucket

    upstreams: every upstream serving this prefix (Config.
    served_upstreams), keyed by its own name; see layout.owner.
    pool: a listing (with ETags) of at least their _pool/ prefixes; pass
    one in when verifying several prefixes, as it can be millions of
    keys. Without, their _pool/ prefixes are listed.
    """
    listing = store.list_etags(listing_root(upstreams.values(), prefix))
    if pool is None:
        pool = list_pool_etags(store, upstreams.values())
    live = state_at(events(store, prefix))
    out: list[SuiteReport] = []
    for report, upstream in _owned(store, upstreams, prefix, out, ''):
        suite = report.suite
        dists = served_dists(upstream, prefix, suite)
        files = {
            n: store.get_bytes(dists + n)
            for n in SIGNATURES
            if dists + n in listing
        }
        release = _release(files, upstream, report)
        if release is None:
            continue
        served = live.get(suite)
        entries = _entries(
            store,
            upstream,
            suite,
            release,
            served.served.ref if served else None,
        )
        check = _Check(store, upstream, dists, report, deep, concurrency)
        check.indexes(release, entries, listing)
        if served is None:
            report.errors.append('no history names this suite')
            continue
        report.ref = str(served.served.ref)
        first = files.get('InRelease') or files.get('Release') or b''
        if hashlib.sha256(first).hexdigest() != served.served.release_sha256:
            report.errors.append(
                'served Release is not the one history recorded'
            )
        check.pool(served.served.ref, entries, pool)
    for report in out:
        if report.fallback:
            report.warnings.insert(
                0,
                (
                    f'{report.fallback} objects checked by recorded sha256 '
                    f'only: uploaded multipart or no MD5 to compare with (a '
                    f'fetch uploads pool objects again as single PUTs)'
                ),
            )
    return out


def _owned(
    store: Store,
    upstreams: dict[str, Upstream],
    prefix: str,
    out: list[SuiteReport],
    how: str,
) -> Iterator[tuple[SuiteReport, Upstream]]:
    """A new report in out for every suite prefix serves, and its upstream

    A suite no configured upstream owns (layout.owner) gets its error
    here, and is not yielded.
    """
    for suite in served_suites(store, upstreams.values(), prefix):
        log.info('%s%s: verifying%s', prefix, suite, how)
        report = SuiteReport(suite)
        out.append(report)
        upstream = owner(upstreams, suite)
        if upstream is None:
            report.errors.append('no configured upstream owns this suite')
            continue
        yield report, upstream


def _release(
    files: dict[str, bytes], upstream: Upstream, report: SuiteReport
) -> Release | None:
    "The Release these signature files carry, verified; None if not"
    report.signatures = sorted(files)
    try:
        return Release.parse(verified_release(files, upstream.keyring))
    except (SignatureError, ValueError) as exc:
        report.errors.append(f'signature: {exc}')
        return None


def _entries(
    store: Store,
    upstream: Upstream,
    suite: str,
    release: Release,
    ref: Ref | None,
) -> list[Entry]:
    """The index files the served snapshot holds, as the Release names them

    What the snapshot took, not what the config would take today: a
    snapshot cut before dep11 was configured is not missing dep11. With
    no history to name the snapshot, the config's selection it is.
    """
    raw = None
    if ref is not None:
        raw = store.get_bytes(
            snapshot_root(upstream.name, suite, ref) + MARKER
        )
    if raw is None:
        return index.wanted(release, upstream, suite)
    return [
        release.entries.get(path) or Entry(path, sha256, size)
        for path, sha256, size in json.loads(raw)['entries']
    ]


def _by_etag(etag: str, md5: str) -> bool:
    "Whether an ETag can be checked against md5 at all"
    return bool(md5) and '-' not in etag


class _Check:
    "The bucket checks of one served suite, each adding to its report"

    def __init__(
        self,
        store: Store,
        upstream: Upstream,
        dists: str,
        report: SuiteReport,
        deep: bool,
        concurrency: int,
    ) -> None:
        self.store = store
        self.upstream = upstream
        self.dists = dists
        self.report = report
        self.deep = deep
        self.concurrency = concurrency

    def indexes(
        self,
        release: Release,
        entries: list[Entry],
        listing: dict[str, tuple[int, str]],
    ) -> None:
        "Every index at its canonical and (if advertised) by-hash path"
        report = self.report
        keys = []
        for entry in entries:
            keys.append((self.dists + entry.path, entry))
            if release.acquire_by_hash:
                by_hash = index.by_hash_dir(entry.path)
                keys.append((f'{self.dists}{by_hash}/{entry.sha256}', entry))
        slow = []
        for key, entry in keys:
            if key not in listing:
                report.errors.append(f'{key}: missing')
                continue
            size, etag = listing[key]
            if size != entry.size:
                report.errors.append(
                    f'{key}: size {size}, Release says {entry.size}'
                )
            elif self.deep or not _by_etag(etag, entry.md5):
                slow.append((key, entry.sha256, entry.size))
                report.fallback += not self.deep
            elif etag != entry.md5:
                report.errors.append(
                    f'{key}: ETag is not the MD5 the Release names'
                )
            else:
                report.indexes += 1
        for key, ok in self._digests(slow, self._index_sha256, 'indexes'):
            if ok:
                report.indexes += 1
            else:
                report.errors.append(
                    f'{key}: sha256 is not the one the Release names'
                )

    def _index_sha256(self, key: str) -> str | None:
        "--deep: hash what is there; else the sha256 recorded at upload"
        if self.deep:
            data = self.store.get_bytes(key) or b''
            return hashlib.sha256(data).hexdigest()
        return self.store.sha256(key)

    def pool(
        self,
        ref: Ref,
        entries: list[Entry],
        listing: dict[str, tuple[int, str]],
    ) -> None:
        "Every pool file the snapshot selected, in _pool/ and intact"
        report = self.report
        root = snapshot_root(self.upstream.name, report.suite, ref)
        raw = self.store.get_bytes(root + 'selected.gz')
        if raw is None:
            report.errors.append(f'snapshot {ref}: selected.gz missing')
            return
        selected = set(gzip.decompress(raw).decode().splitlines())
        wanted = self._named(entries, selected)
        for filename in sorted(selected - set(wanted)):
            report.warnings.append(
                f'{filename}: selected at cut, not in the '
                f'served Packages or Sources'
            )
        slow = []
        for filename, (size, sha256, md5) in sorted(wanted.items()):
            key = storage_key(self.upstream, filename)
            if key not in listing:
                report.errors.append(f'{key}: missing from the pool')
                continue
            have_size, etag = listing[key]
            if have_size != size:
                report.errors.append(
                    f'{key}: size {have_size}, Packages says {size}'
                )
            elif not _by_etag(etag, md5):
                slow.append((key, sha256, size))
                report.fallback += 1
            elif etag != md5:
                report.errors.append(
                    f'{key}: ETag is not the MD5 Packages names'
                )
            elif self.deep:
                slow.append((key, sha256, size))
            else:
                report.pool += 1
        for key, ok in self._digests(slow, self.store.sha256, 'pool'):
            if ok:
                report.pool += 1
            else:
                report.errors.append(
                    f'{key}: recorded sha256 is not the one Packages names'
                )

    def _named(
        self, entries: list[Entry], selected: set[str]
    ) -> dict[str, tuple[int, str, str]]:
        "filename -> (size, sha256, md5) of the selected files indexes name"
        out = {}
        for stanza in self._stanzas(index.packages_indexes(entries)):
            if stanza.get('Filename') in selected:
                out[stanza['Filename']] = (
                    int(stanza['Size']),
                    stanza['SHA256'],
                    stanza.get('MD5sum', ''),
                )
        for stanza in self._stanzas(index.sources_indexes(entries)):
            md5s = index.source_md5s(stanza)
            for filename, size, sha256 in index.source_files(stanza):
                if filename in selected:
                    out[filename] = (size, sha256, md5s.get(filename, ''))
        return out

    def _stanzas(self, indexes: dict[str, Entry]) -> Iterator[dict[str, str]]:
        "Every stanza of these indexes, skipping one whose hash is wrong"
        for entry in indexes.values():
            data = self.store.get_bytes(self.dists + entry.path) or b''
            if hashlib.sha256(data).hexdigest() != entry.sha256:
                continue  # already reported by indexes()
            with index.open_text(entry.path, data) as fh:
                yield from stanzas(fh)

    def _digests(
        self,
        jobs: list[tuple[str, str, int]],
        digest: Callable[[str], str | None],
        what: str,
    ) -> Iterator[tuple[str, bool]]:
        """(key, whether digest(key) is the sha256) of every job, in order

        A job is (key, sha256, size). With progress: these go one
        object at a time.
        """
        if not jobs:
            return
        progress = Progress(
            sum(size for _, _, size in jobs), len(jobs), f'{self.dists} {what}'
        )

        def one(job):
            key, sha256, size = job
            ok = digest(key) == sha256
            progress(size)
            progress.item_done()
            return key, ok

        try:
            with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
                yield from ex.map(one, jobs)
        finally:
            progress.close()


def verify_http(
    http: httpx.Client,
    base: str,
    upstreams: dict[str, Upstream],
    prefix: str,
    store: Store,
    sample: int = 20,
    pool: dict[str, tuple[int, str]] | None = None,
) -> list[SuiteReport]:
    """What apt gets from the front end, for every suite of a prefix

    The suites and the pool files to sample come from the bucket; the
    bytes checked come over HTTP from base (http://aptberg.example.com).
    upstreams and pool: as in verify_prefix.
    """
    if pool is None:
        pool = list_pool_etags(store, upstreams.values())
    base = base.rstrip('/')
    out: list[SuiteReport] = []
    for report, upstream in _owned(
        store, upstreams, prefix, out, ' over HTTP'
    ):
        url = f'{base}/{served_dists(upstream, prefix, report.suite)}'
        files = {}
        for name in SIGNATURES:
            resp = http.get(url + name)
            if resp.status_code == 200:
                files[name] = resp.content
            elif resp.status_code != 404:
                report.errors.append(f'{url}{name}: HTTP {resp.status_code}')
        release = _release(files, upstream, report)
        if release is None:
            continue
        sizes = _http_indexes(http, url, release, upstream, report)
        mirrored = sorted(f for f in sizes if storage_key(upstream, f) in pool)
        rng = random.Random(f'{prefix}{report.suite}')
        picked = rng.sample(mirrored, min(sample, len(mirrored)))
        _http_pool(http, f'{base}/{prefix}', picked, sizes, report)
    return out


def _http_indexes(
    http: httpx.Client,
    url: str,
    release: Release,
    upstream: Upstream,
    report: SuiteReport,
) -> dict[str, int]:
    """Fetch and check every Packages and Sources index over HTTP

    Returns filename -> size of every pool file they name.
    """
    entries = index.wanted(release, upstream, report.suite)
    out = {}
    for _, entry in sorted(
        {
            **index.packages_indexes(entries),
            **index.sources_indexes(entries),
        }.items()
    ):
        path = (
            f'{index.by_hash_dir(entry.path)}/{entry.sha256}'
            if release.acquire_by_hash
            else entry.path
        )
        resp = http.get(url + path)
        if resp.status_code == 404 and index.optional(entry.path):
            continue  # never served upstream either
        if resp.status_code != 200:
            report.errors.append(f'{url}{path}: HTTP {resp.status_code}')
            continue
        if hashlib.sha256(resp.content).hexdigest() != entry.sha256:
            report.errors.append(
                f'{url}{path}: sha256 is not the one the Release names'
            )
            continue
        report.indexes += 1
        with index.open_text(entry.path, resp.content) as fh:
            for stanza in stanzas(fh):
                if 'Filename' in stanza:
                    out[stanza['Filename']] = int(stanza['Size'])
                else:
                    for f, size, _ in index.source_files(stanza):
                        out[f] = size
    return out


def _http_pool(
    http: httpx.Client,
    url: str,
    filenames: list[str],
    sizes: dict[str, int],
    report: SuiteReport,
) -> None:
    "Request pool files through the front end's rewrite below url"
    for filename in filenames:
        where = f'{url}{served_name(filename)}'
        resp = http.head(where)
        length = resp.headers.get('content-length')
        if resp.status_code != 200:
            report.errors.append(f'{where}: HTTP {resp.status_code}')
        elif length is not None and int(length) != sizes[filename]:
            report.errors.append(
                f'{where}: length {length}, Packages says {sizes[filename]}'
            )
        else:
            report.pool += 1
