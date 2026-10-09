"""Sync: load suites, upload the pool, cut -- one pipeline, three commands

fetch, cut and sync each run part of the same pipeline:

    load      fetch every suite from upstream into scratch (or, for
              cut, reload what scratch holds), verified
    select    the pool files the filters and their dependency closure
              want (pool.select)
    upload    put what _pool/ lacks there
    cut       snapshot each suite into _snap/ and point the cur and acc
              manifests at it

Each step builds on what the one before learned. The expensive thing
they share is the _pool/ listing: millions of keys on a full mirror. A
Sync lists it at most once and keeps it current as it uploads, so the
cut (and the apply after it) never list it again.
"""

import logging
import os
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import pool, snapshot
from .config import Config, Upstream
from .fetch import FetchedSuite, client, fetch_suite, load_suite, probe_family
from .manifest import Manifest
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Scope:
    "What the command line asked for; empty means everything"

    upstreams: tuple[str, ...] = ()  # each group already whole
    suites: tuple[str, ...] = ()
    codenames: tuple[str, ...] = ()
    channels: tuple[str, ...] = ()

    def names(self, upstream: Upstream) -> bool:
        "Whether the upstream is one the command is for"
        return not self.upstreams or upstream.name in self.upstreams

    def wants(self, fs: FetchedSuite) -> bool:
        "Whether a loaded suite is one to cut"
        up = fs.upstream
        if not self.names(up):
            return False
        if (self.suites or self.codenames) and not (
            fs.suite in self.suites or fs.family in self.codenames
        ):
            return False
        if self.channels:
            trees = {
                up.channels[c].tree for c in self.channels if c in up.channels
            }
            return fs.source.tree in trees
        return True


@dataclass
class Cut:
    "What cutting one suite came to"

    name: str  # upstream [tree] suite
    result: snapshot.CutResult | None = None
    error: str = ''  # why it was refused, if it was
    manifests: list[Manifest] = field(default_factory=list)


class Sync:
    """The pipeline for one command run

    store is None only for fetch --no-pool, which never gets as far as
    the bucket.
    """

    def __init__(self, cfg: Config, store: Store | None, scope: Scope):
        self.cfg = cfg
        self.store = store
        self.scope = scope
        self.suites: list[FetchedSuite] = []
        self.selection = pool.Selection()
        # The _pool/ listing (key -> size, ETag) once something listed
        # it; kept current with every upload.
        self.listing: dict[str, tuple[int, str]] | None = None
        # Without one, what present() listed for the cut (key -> size).
        self.sizes: dict[str, int] | None = None

    def load(self, offline: bool) -> None:
        """Fetch the suites (or reload them from scratch), and select

        All suites, or just those that can matter to the named
        upstreams. What a filtered suite selects comes from the
        dependency closure of its family (codename): every suite sharing
        the codename, and every suite whose codename no filtered
        upstream lists (they seed all families). So naming a filtered
        upstream still loads those, but not the other families, which
        cannot change the outcome; a Release is all that is looked at to
        tell. An unfiltered upstream is mirrored whole whatever else
        exists, so naming only those loads only them.
        """
        cfg = self.cfg
        http = client(cfg.concurrency, cfg.user_agent, cfg.download_rate)
        jobs = [
            (up, source, suite)
            for up in cfg.upstreams.values()
            for source in up.sources()
            for suite in up.suites
        ]
        if self.scope.upstreams:
            jobs = self._that_matter(jobs, None if offline else http)
        start = time.monotonic()
        if offline:
            log.info(
                'loading %d suites from %s, verifying every index',
                len(jobs),
                cfg.scratch,
            )
            suites = [load_suite(*job, cfg.scratch) for job in jobs]
            log.info(
                'loaded %d suites (%.1fs)',
                len(suites),
                time.monotonic() - start,
            )
        else:
            log.info('fetching %d suites', len(jobs))
            run = (
                f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-'
                f'{socket.gethostname()}-{os.getpid()}'
            )
            suites = [
                fetch_suite(http, *job, cfg.scratch, run) for job in jobs
            ]
        self.use(suites)

    def use(self, suites: list[FetchedSuite]) -> None:
        "Work on suites loaded elsewhere (backfill does), and select"
        self.suites = suites
        self.selection = pool.select(suites)

    def _that_matter(self, jobs: list[tuple], http) -> list[tuple]:
        "The (upstream, source, suite) jobs the named upstreams need"
        log.info(
            'checking the Release of %d suites to see which matter', len(jobs)
        )
        family = [probe_family(http, *job, self.cfg.scratch) for job in jobs]
        filtered = {
            family[i]
            for i, job in enumerate(jobs)
            if job[0].filter is not None
        }
        named = {i for i, job in enumerate(jobs) if self.scope.names(job[0])}
        deps = {family[i] for i in named if jobs[i][0].filter is not None}
        keep = [
            job
            for i, job in enumerate(jobs)
            if i in named
            or (deps and (family[i] in deps or family[i] not in filtered))
        ]
        if len(keep) < len(jobs):
            log.info(
                'skipping %d suites that cannot matter', len(jobs) - len(keep)
            )
        return keep

    def named_upstreams(self) -> list[Upstream]:
        """The upstreams of the loaded suites that the command is for

        Suites loaded only for a filtered upstream's dependency closure
        belong to others: their pools are not uploaded to or cut from.
        """
        return [
            fs.upstream for fs in self.suites if self.scope.names(fs.upstream)
        ]

    def pool_selection(self) -> pool.Selection:
        "The pool files to upload: the named upstreams' share"
        if self.scope.upstreams:
            return self.selection.restrict(self.scope.upstreams, self.suites)
        return self.selection

    def list_pool(self) -> None:
        "List the named upstreams' _pool/, unless that happened already"
        if self.listing is None:
            self.listing = pool.list_etags(self.store, self.named_upstreams())

    def upload(self, dry_run: bool) -> pool.SyncResult:
        """Upload the pool files _pool/ lacks

        Short of dry_run, the listing is updated with every file just
        confirmed present, so a cut building on it next sees the upload
        without listing millions of keys again to find out.
        """
        selection = self.pool_selection()
        self.list_pool()
        cfg = self.cfg
        result = pool.sync(
            selection,
            self.store,
            client(cfg.concurrency, cfg.user_agent, cfg.download_rate),
            cfg.scratch / '.pool-tmp',
            cfg.concurrency,
            dry_run=dry_run,
            present=self.listing,
        )
        if not dry_run:
            failed = {key for key, _ in result.failed}
            self.listing.update(
                {
                    item.key: (item.size, '')
                    for item in selection.items.values()
                    if item.key not in failed
                }
            )
        return result

    def present(self, upstreams: list[Upstream]) -> dict[str, int]:
        """_pool/ key -> size, for (at least) these upstreams

        From the listing if there is one; else their _pool/ is listed,
        once: the apply after a cut asks again, for a subset.
        """
        if self.listing is not None:
            return {key: size for key, (size, _) in self.listing.items()}
        if self.sizes is None:
            self.sizes = pool.list_sizes(self.store, upstreams)
        return self.sizes

    def cut(
        self,
        allow_missing: bool = False,
        dry_run: bool = False,
        stages: list[str] | None = None,
    ) -> Iterator[Cut]:
        """Cut every wanted suite, recording each in its manifests

        One Cut per suite, as it is done. A suite that is refused (its
        pool files are not all in _pool/, say) does not stop the rest.
        """
        targets = self._targets()
        if self.cfg.manifests is None:
            log.warning(
                'no manifests: in config; cur/acc manifests not updated'
            )
        present = self.present([fs.upstream for fs in targets])
        for fs in targets:
            name = f'{fs.upstream.name} {fs.source.tree or ""} {fs.suite}'
            out = Cut(' '.join(name.split()))
            try:
                out.result = snapshot.cut(
                    fs,
                    self.selection,
                    self.store,
                    present,
                    allow_missing=allow_missing,
                    dry_run=dry_run,
                )
            except snapshot.CutError as exc:
                out.error = str(exc)
                yield out
                continue
            if self.cfg.manifests is not None and not dry_run:
                out.manifests = snapshot.record(
                    out.result,
                    self.cfg.manifests,
                    self.cfg.bucket,
                    self.scope.channels or None,
                    stages,
                )
            yield out

    def _targets(self) -> list[FetchedSuite]:
        "The suites to cut; refuses a selection that cannot be cut"
        targets = [fs for fs in self.suites if self.scope.wants(fs)]
        unmatched = [
            c
            for c in self.scope.codenames
            if not any(fs.family == c for fs in targets)
        ]
        if unmatched:
            raise snapshot.CutError(
                f'no suite with codename {", ".join(unmatched)} among the '
                f'selected upstreams'
            )
        if not targets:
            raise snapshot.CutError('nothing matches the given selection')
        _same_run(self.suites, {fs.upstream.unit for fs in targets})
        return targets


def _same_run(suites: list[FetchedSuite], units: set[str]) -> None:
    """Refuse to cut a group from indexes of different fetch runs

    A fetch that died half way leaves some suites of a group new and
    some old; cutting that would pair a new ubuntu with a stale
    ubuntu-security. Ungrouped upstreams are a group of one.
    """
    for unit in sorted(units):
        runs = {fs.run for fs in suites if fs.upstream.unit == unit}
        if len(runs) > 1:
            raise snapshot.CutError(
                f'{unit}: scratch holds indexes of different fetch runs '
                f'({", ".join(sorted(r or "?" for r in runs))}); fetch '
                f'again (or use sync) before cutting'
            )
