"""Which pool files to mirror, and getting them into the global pool

Every .deb lives in the _pool/<name>/ of its upstream: its unit (group or
name) unless its pool: names another, like _shared for upstreams
that mean to share one. Within a pool a Filename: is the key, as every
archive's own release process guarantees is safe; across archives run by
different organizations it is not -- Debian and Ubuntu reuse the same
name_version_arch for binaries with different content often enough that
it is the rule, not the exception (docs/FINDINGS.rst) -- so nothing shares a
pool by default. An HTTP rewrite in front of the bucket serves
/<upstream>/ch/<prefix>/pool/<path> from the resolved _pool/ key, so the
Filename: of a signed Packages index still resolves for every channel
prefix. A flat upstream names its files ./<path>, served from
/<upstream>/ch/<prefix>/<path> instead (see layout.py).

A filter picks the seed names (include patterns minus soft excludes),
the dependency closure adds what they need, and hard excluded names are
never taken (see config.Filter).
Selection runs per family (release.family of the codename): the
dependency closure spans all suites sharing one across all upstreams
(noble, noble-updates, noble-security, zabbix noble, debian bookworm and
its -security/-backports/-updates pockets, ...), plus suites whose family
no filtered upstream uses (anydist), which join every family. Packages of
unfiltered upstreams are mirrored whole and also seed the closure, so the
Ubuntu libraries they depend on are mirrored.
"""

import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkstemp

import httpx

from . import index
from .config import Upstream
from .deb822 import stanzas
from .fetch import FetchedSuite, FetchError, download
from .filter import (
    Closure,
    Index,
    closure,
    is_glob,
    matches,
    matching,
    relations,
)
from .lock import fetching
from .progress import Progress
from .store import IntegrityError, Store

log = logging.getLogger(__name__)

PREFIX = '_pool/'


def storage_key(upstream: Upstream, filename: str) -> str:
    """The _pool/ object key a Filename: maps to for this upstream

    It is also what Selection.items is keyed by: upstreams that do not
    share a pool never collide, and ones that do share one dedup.
    """
    name = filename.removeprefix('./').removeprefix('pool/')
    return f'{PREFIX}{upstream.pool_name}/{name}'


def served_name(filename: str) -> str:
    "A Filename: relative to the channel prefix it is served below"
    return filename.removeprefix('./')


def prefixes(upstreams: Iterable[Upstream]) -> list[str]:
    "The _pool/ key prefixes holding the files of these upstreams"
    return sorted({f'{PREFIX}{up.pool_name}/' for up in upstreams})


def list_sizes(store: Store, upstreams: Iterable[Upstream]) -> dict[str, int]:
    "Every _pool/ file of these upstreams, with its size"
    return _list(upstreams, store.list_sizes)


def list_etags(
    store: Store, upstreams: Iterable[Upstream]
) -> dict[str, tuple[int, str]]:
    "Every _pool/ file of these upstreams, with its size and ETag"
    return _list(upstreams, store.list_etags)


def _list(
    upstreams: Iterable[Upstream], lister: Callable[[str], dict]
) -> dict:
    """The _pool/ files of these upstreams, as lister gives them

    Only their own _pool/<name>/ is listed, not the whole pool: a full
    mirror is millions of keys, most of them somebody else's. Callers
    that need it more than once (one run applying several plans, say)
    list once and pass the result on.
    """
    listing: dict = {}
    for prefix in prefixes(upstreams):
        log.info('%s: listing', prefix)
        start = time.monotonic()
        found = lister(prefix)
        log.info(
            '%s: %d objects (%.1fs)',
            prefix,
            len(found),
            time.monotonic() - start,
        )
        listing.update(found)
    return listing


class PoolConflict(Exception):
    "One pool path, two different contents"


@dataclass(frozen=True, slots=True)
class PoolItem:
    "One pool file, where to fetch it, and its resolved _pool/ key"

    filename: str
    size: int
    sha256: str
    url: str
    key: str


@dataclass
class Selection:
    """The pool files to mirror, and per family how the filter fared

    items is keyed by storage_key(), not by the bare Filename:, so two
    upstreams that do not share a pool can each hold an item under the
    same Filename: without colliding.
    """

    items: dict[str, PoolItem] = field(default_factory=dict)
    closures: dict[str, Closure] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        "The size of everything selected"
        return sum(item.size for item in self.items.values())

    def add(
        self, stanza: dict[str, str], url: str, upstream: Upstream
    ) -> None:
        "Add the pool file a stanza names; conflicting content is fatal"
        filename = stanza['Filename']
        self._add_item(
            filename, int(stanza['Size']), stanza['SHA256'], url, upstream
        )

    def add_source(
        self, stanza: dict[str, str], url: str, upstream: Upstream
    ) -> None:
        "Add every file a Sources stanza names; conflicting content is fatal"
        for filename, size, sha256 in index.source_files(stanza):
            self._add_item(filename, size, sha256, url, upstream)

    def _add_item(
        self,
        filename: str,
        size: int,
        sha256: str,
        url: str,
        upstream: Upstream,
    ) -> None:
        if upstream.flat:
            # ./x.deb beside the Release; nothing that climbs out of it
            ok = filename.startswith('./') and '..' not in filename.split('/')
            where = 'not ./<path> below the repository'
        else:
            ok = filename.startswith('pool/')
            where = 'outside pool/'
        if not ok:
            raise PoolConflict(
                f'{url}: Filename {filename!r} is {where}; the pool '
                f'rewrite cannot serve it'
            )
        item = PoolItem(
            filename,
            size,
            sha256,
            f'{url}/{served_name(filename)}',
            storage_key(upstream, filename),
        )
        have = self.items.setdefault(storage_key(upstream, filename), item)
        if have.sha256 != item.sha256:
            raise PoolConflict(
                f'{filename}: {have.url} has sha256 {have.sha256}, '
                f'{item.url} has {item.sha256}'
            )

    def restrict(
        self, upstreams: Iterable[str], suites: list[FetchedSuite]
    ) -> 'Selection':
        "Only the items at least one suite of the given upstreams names"
        names = set(upstreams)
        keep = set()
        for fs in suites:
            if fs.upstream.name in names:
                keep.update(
                    storage_key(fs.upstream, s['Filename'])
                    for _, s in _packages(fs, ('Filename',))
                )
                for _, stanza in _sources(fs, SOURCE_FIELDS):
                    keep.update(
                        storage_key(fs.upstream, f)
                        for f, _, _ in index.source_files(stanza)
                    )
        return Selection(
            {k: v for k, v in self.items.items() if k in keep}, self.closures
        )


SOURCE_FIELDS = ('Package', 'Binary', 'Directory', 'Checksums-Sha256')


def _binary_names(value: str) -> set[str]:
    "The flat set of binary package names a Sources Binary: field lists"
    return {name for alts in relations(value) for name in alts}


def select(suites: list[FetchedSuite]) -> Selection:
    """Decide the pool files to mirror for a set of fetched suites

    An unfiltered upstream is mirrored whole. Filtered ones are decided
    per family (codename): the dependency closure of what their filters
    seed it with, over every suite of the family plus the suites of no
    filtered family, which belong to every family.
    """
    log.info('selecting pool files from the indexes of %d suites', len(suites))
    start = time.monotonic()
    out = Selection()
    for fs in suites:
        if fs.upstream.filter is None:
            _add_whole(out, fs)
    codenames = sorted(
        {fs.family for fs in suites if fs.upstream.filter is not None}
    )
    loose = [fs for fs in suites if fs.family not in codenames]
    for codename in codenames:
        family = _Family(
            [fs for fs in suites if fs.family == codename] + loose
        )
        result = family.resolve()
        out.closures[codename] = result
        family.add_wanted(out, result.wanted)
        log.info(
            '%s: %d names wanted, %d listed names absent, '
            '%d unsatisfied hard dependencies',
            codename,
            len(result.wanted),
            len(result.absent),
            len(result.missing_hard()),
        )
    log.info(
        'selected %d pool files (%.1fs)',
        len(out.items),
        time.monotonic() - start,
    )
    return out


def _add_whole(out: Selection, fs: FetchedSuite) -> None:
    "Every pool file an unfiltered suite names"
    for url, stanza in _packages(fs, ('Filename', 'Size', 'SHA256')):
        out.add(stanza, url, fs.upstream)
    if fs.upstream.deb_src:
        for url, stanza in _sources(fs, SOURCE_FIELDS):
            out.add_source(stanza, url, fs.upstream)


class _Family:
    """The suites of one family, indexed, and what their filters seed

    A suite without a filter seeds every name it has; a filtered one
    its literal includes and its glob matches, minus what a soft exclude
    drops. Hard excludes block names outright.
    """

    def __init__(self, members: list[FetchedSuite]) -> None:
        self.members = members
        # The dependency fields any filter of the family follows.
        self.follow: list[str] = []
        for fs in members:
            if fs.upstream.filter is not None:
                self.follow += [
                    f
                    for f in fs.upstream.filter.follow
                    if f not in self.follow
                ]
        self.index = Index()
        self.seeds: set[str] = set()  # literal includes and glob matches
        self.soft: set[str] = set()  # glob matches a soft exclude dropped
        self.hard: set[str] = set()
        self.unmatched: set[str] = set()  # globs that matched nothing
        for fs in members:
            self._read(fs)

    def _read(self, fs: FetchedSuite) -> None:
        "Index a suite's Packages, and seed from them"
        fields = ('Package', 'Provides', 'Filename', 'Size', 'SHA256')
        names = set()
        for url, stanza in _packages(fs, (*fields, *self.follow)):
            stanza['_url'] = url
            stanza['_upstream'] = fs.upstream
            self.index.add(stanza)
            names.add(stanza['Package'])
        filt = fs.upstream.filter
        if filt is None:
            self.seeds |= names
            return
        self.seeds.update(p for p in filt.include if not is_glob(p))
        per = matches(names, [p for p in filt.include if is_glob(p)])
        self.unmatched.update(p for p, found in per.items() if not found)
        matched = set().union(*per.values())
        dropped = matching(matched, filt.soft_exclude)
        self.seeds |= matched - dropped
        self.soft |= dropped
        self.hard |= matching(names, filt.hard_exclude)

    def resolve(self) -> Closure:
        "The dependency closure of the seeds, with what fell outside it"
        result = closure(self.index, self.seeds, self.follow, self.hard)
        result.excluded = self.soft - self.seeds
        # A glob matching nothing in this family is reported like an
        # absent name; in another family it may well match.
        result.absent |= {
            p for p in self.unmatched if not matching(self.index.stanzas, [p])
        }
        return result

    def add_wanted(self, out: Selection, wanted: set[str]) -> None:
        "The pool files of the wanted names, and of their sources"
        for name in wanted:
            for stanza in self.index.stanzas[name]:
                out.add(stanza, stanza['_url'], stanza['_upstream'])
        for fs in self.members:
            if not fs.upstream.deb_src:
                continue
            for url, stanza in _sources(fs, SOURCE_FIELDS):
                if _binary_names(stanza.get('Binary', '')) & wanted:
                    out.add_source(stanza, url, fs.upstream)


def _packages(
    fs: FetchedSuite, fields: Iterable[str]
) -> Iterator[tuple[str, dict[str, str]]]:
    "Slimmed stanzas of every Packages index of a fetched suite"
    fields = tuple(fields)
    url = fs.source.url
    for _, entry in sorted(index.packages_indexes(fs.entries).items()):
        with index.open_text(fs.path / entry.path) as fh:
            for stanza in stanzas(fh):
                yield url, {k: stanza[k] for k in fields if k in stanza}


def _sources(
    fs: FetchedSuite, fields: Iterable[str]
) -> Iterator[tuple[str, dict[str, str]]]:
    "Slimmed stanzas of every Sources index of a fetched suite"
    fields = tuple(fields)
    for _head, entry in sorted(index.sources_indexes(fs.entries).items()):
        with index.open_text(fs.path / entry.path) as fh:
            for stanza in stanzas(fh):
                yield (
                    fs.source.url,
                    {k: stanza[k] for k in fields if k in stanza},
                )


@dataclass
class SyncResult:
    "What a pool sync did"

    selected: int = 0
    present: int = 0
    todo: int = 0
    todo_bytes: int = 0
    redo: int = 0  # present, but uploaded multipart: again
    uploaded: int = 0
    uploaded_bytes: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def sync(
    selection: Selection,
    store: Store,
    http: httpx.Client,
    tmpdir: Path,
    concurrency: int = 8,
    dry_run: bool = False,
    progress: bool | None = None,
    *,
    present: dict[str, tuple[int, str]],
) -> SyncResult:
    """Upload every selected pool file _pool/ lacks

    What is present is the caller's listing of the _pool/ of the
    upstreams selected (a full mirror holds millions of keys, so it is
    never listed whole, and callers doing more with _pool/ in the same
    run -- cut's completeness check, apply's plan -- share theirs). A
    present key of the right size is trusted: only aptberg writes
    _pool/, and only after verifying the sha256. A present key of
    another size is a conflict and is never overwritten; older
    snapshots name those bytes.

    A present key uploaded multipart (its ETag ends in -N, so it is not
    the content's MD5) is uploaded again as a single PUT, provided its
    recorded sha256 is the one the index names: the same bytes, now with
    an ETag the server vouches for.

    Failures (upstream 404s, hash mismatches) are collected rather than
    raised, so one bad file does not stop a run of many hours.
    """
    result = SyncResult(selected=len(selection.items))
    todo = []
    redo: set[str] = set()
    for item in selection.items.values():
        size, etag = present.get(item.key, (None, ''))
        if size is None:
            todo.append(item)
        elif size == item.size and '-' in etag:
            todo.append(item)
            redo.add(item.key)
        elif size == item.size:
            result.present += 1
        else:
            result.failed.append(
                (
                    item.key,
                    (
                        f'present with size {size}, {item.url} has '
                        f'{item.size}; refusing to overwrite'
                    ),
                )
            )
    result.todo = len(todo)
    result.redo = len(redo)
    result.todo_bytes = sum(item.size for item in todo)
    if dry_run or not todo:
        return result

    tmpdir.mkdir(parents=True, exist_ok=True)
    for stale in tmpdir.glob('*.deb*'):
        stale.unlink()
    todo.sort(key=lambda item: item.filename)
    with fetching(store):
        _transfer_all(
            todo, redo, result, store, http, tmpdir, concurrency, progress
        )
    return result


def _transfer_all(
    todo: list[PoolItem],
    redo: set[str],
    result: SyncResult,
    store: Store,
    http: httpx.Client,
    tmpdir: Path,
    concurrency: int,
    progress: bool | None,
) -> None:
    bar = Progress(result.todo_bytes, len(todo), 'pool', enabled=progress)
    pool = ThreadPoolExecutor(max_workers=concurrency)
    try:
        futures = {
            pool.submit(
                _transfer, item, item.key in redo, store, http, tmpdir, bar
            ): item
            for item in todo
        }
        try:
            for future in as_completed(futures):
                item = futures[future]
                try:
                    future.result()
                except (FetchError, OSError, IntegrityError) as exc:
                    # Per-file trouble. Anything else (S3 refusing us,
                    # ^C) ends the run below.
                    log.error('%s: %s', item.key, exc)
                    result.failed.append((item.key, str(exc)))
                else:
                    result.uploaded += 1
                    result.uploaded_bytes += item.size
                bar.item_done()
        except KeyboardInterrupt:
            running = sum(1 for f in futures if f.running())
            if running:
                log.warning(
                    'interrupted; waiting for %d transfer(s) already in '
                    'flight to finish (^C again to abandon them)',
                    running,
                )
            raise
    finally:
        # On error, drop what has not started; running transfers finish,
        # unless a second ^C says to abandon them -- a PUT is
        # all-or-nothing, so an abandoned upload just never happened.
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            bar.close()


def _transfer(
    item: PoolItem,
    redo: bool,
    store: Store,
    http: httpx.Client,
    tmpdir: Path,
    progress: Progress,
) -> None:
    if redo and store.sha256(item.key) != item.sha256:
        raise FetchError(
            f'{item.key}: present (multipart) with another '
            f'recorded sha256 than {item.url}; refusing to '
            f'overwrite'
        )
    fd, name = mkstemp(dir=tmpdir, suffix='.deb')
    os.close(fd)
    path = Path(name)
    try:
        if not download(
            http, item.url, path, item.size, item.sha256, progress
        ):
            raise FetchError(f'{item.url}: not found upstream')
        store.put_file(item.key, path, item.sha256)
    finally:
        path.unlink(missing_ok=True)
