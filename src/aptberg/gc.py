"""Garbage collection: the only thing in aptberg that deletes content

Two passes, both dry runs unless told otherwise.

Index pass, per channel prefix (<upstream>/ch/<prefix>/dists/): an
object is live if any snapshot the prefix served at any moment within
the grace (--grace, 7d) names it, by-hash or canonical path. The grace
therefore runs from when an object stopped being served, taken from
_history/, not from its age: a by-hash object dereferenced by today's
flip stays for the whole grace, for clients still holding the previous
InRelease. The top-level InRelease, Release and Release.gpg of a suite
are never collected; apply owns them. A prefix that serves something
but has no history is refused rather than guessed at.

A prefix no configured channel and stage names any more is
unconfigured: dropping a channel from the config is how it is declared
gone, and nothing would ever call its last snapshot unserved. Left
alone otherwise (it is only reported), with drop_unconfigured everything
under it is dead at once -- the grace does not apply, no history is
read. A config that lost a live channel by mistake would be wiped, hence
opt-in.

Pool pass, global (_pool/): a .deb is live while any snapshot in _snap/
names it in its filenames.gz, whether or not a manifest points at it, so
a rollback or a late promote always finds its files. Pool files only
become dead by retiring snapshots. fetch uploads pool files well before
the cut that references them, so the pass refuses while a fetch holds a
marker under _lock/fetch/, and skips files younger than the grace anyway.

Never touched: _snap/, _history/, _lock/.
"""

import gzip
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import index
from .config import Upstream
from .fetch import SIGNATURES
from .history import events, state_at
from .layout import dists as served_dists
from .layout import listing_root, owner
from .lock import FETCH as FETCH_LOCKS
from .manifest import STAGES, Ref, prefix_name
from .pool import PREFIX as POOL_PREFIX
from .pool import storage_key
from .snapshot import MARKER, SNAP, snapshot_root
from .store import Store

log = logging.getLogger(__name__)

# Seconds between progress lines in a long loop
LOG_EVERY = 30.0
# Keys deleted per progress line
DELETE_CHUNK = 10000


class GCError(Exception):
    "Collection is not safe to run"


@dataclass
class Collection:
    "What one pass would delete"

    scope: str
    dead: dict[str, int] = field(default_factory=dict)  # key -> size
    kept: int = 0
    young: int = 0
    unconfigured: bool = False  # no configured channel names the prefix

    @property
    def dead_bytes(self) -> int:
        "The bytes the dead objects hold"
        return sum(self.dead.values())


def served_within(
    store: Store, prefix: str, grace: timedelta, now: datetime
) -> dict[str, set[Ref]]:
    "suite -> every ref the prefix served at some moment within grace"
    history = events(store, prefix)
    start = now - grace
    out: dict[str, set[Ref]] = {}
    for suite, live in state_at(history, start).items():
        out.setdefault(suite, set()).add(live.served.ref)
    for event in history:
        if event.at > start:
            for suite, served in event.suites.items():
                out.setdefault(suite, set()).add(served.ref)
    return out


def is_unconfigured(upstreams: dict[str, Upstream], prefix: str) -> bool:
    "Whether no configured channel and stage of these upstreams is prefix"
    name = prefix.rstrip('/').rpartition('/')[2]
    return not any(
        name == prefix_name(channel, stage)
        for up in upstreams.values()
        for channel in (up.channels or [None])
        for stage in STAGES
    )


def unconfigured_pass(store: Store, prefix: str) -> Collection:
    "Every object under a prefix the config no longer has"
    out = Collection(prefix)
    log.info('%s: not in the config', prefix)
    out.dead = _listed(prefix, 'everything', store.list_sizes, prefix)
    return out


def index_pass(
    store: Store,
    upstreams: dict[str, Upstream],
    prefix: str,
    grace: timedelta,
    now: datetime | None = None,
    drop_unconfigured: bool = False,
) -> Collection:
    """The dead index objects under one channel prefix

    upstreams: every upstream serving at this prefix (Config.
    served_upstreams), keyed by its own name -- usually one, more than
    one when an upstream's path: puts it at another's served location.
    Whose each suite is: layout.owner.
    """
    unconfigured = is_unconfigured(upstreams, prefix)
    if unconfigured and drop_unconfigured:
        return unconfigured_pass(store, prefix)
    now = now or datetime.now(timezone.utc)
    out = Collection(prefix, unconfigured=unconfigured)
    root = listing_root(upstreams.values(), prefix)
    what = root.removeprefix(prefix) or 'all'
    listing = _listed(prefix, what, store.list_sizes, root)
    if not listing:
        return out
    if unconfigured:
        out.kept = len(listing)
        return out
    refs = served_within(store, prefix, grace, now)
    if not refs:
        raise GCError(
            f'{prefix} serves files but has no history; not collecting it'
        )
    live, served = _live(store, upstreams, prefix, refs)
    for key, size in listing.items():
        if key in live:
            out.kept += 1
        elif not any(key.startswith(d) for d in served):
            # A suite the history never mentions: not ours to judge.
            out.kept += 1
        else:
            out.dead[key] = size
    log.info('%s: %d live, %d dead', prefix, out.kept, len(out.dead))
    return out


def _live(
    store: Store,
    upstreams: dict[str, Upstream],
    prefix: str,
    refs: dict[str, set[Ref]],
) -> tuple[set[str], set[str]]:
    """The live keys under prefix, and the dists/<suite>/ they cover

    Live: what any of refs (suite -> refs served within the grace)
    names, by canonical and by-hash path, and every suite's signature
    files. A suite no configured upstream owns is left out of both.
    """
    live = set()
    log.info(
        '%s: reading the snapshots of %d suites served within the grace',
        prefix,
        len(refs),
    )
    served = set()
    for suite, suite_refs in refs.items():
        upstream = owner(upstreams, suite)
        if upstream is None:
            continue  # same as a suite history never mentions at all
        name = upstream.name
        dists = served_dists(upstream, prefix, suite)
        served.add(dists)
        live.update(dists + sig for sig in SIGNATURES)
        for ref in suite_refs:
            root = snapshot_root(name, suite, ref)
            raw = store.get_bytes(root + MARKER)
            if raw is None:
                raise GCError(
                    f'{prefix}: served {suite} {ref} within the grace, but '
                    f'{root} is gone (retired with a shorter --grace?)'
                )
            for path, sha256, _ in json.loads(raw)['entries']:
                live.add(dists + path)
                live.add(f'{dists}{index.by_hash_dir(path)}/{sha256}')
            log.debug(
                '%s: %s %s: %d live keys so far', prefix, suite, ref, len(live)
            )
    return live, served


def pool_pass(
    store: Store,
    grace: timedelta,
    upstreams: dict[str, Upstream],
    now: datetime | None = None,
    force: bool = False,
) -> Collection:
    """The dead objects in _pool/

    Each _snap/<upstream>/... filenames.gz is resolved to _pool/ keys
    through its own upstream's pool, not one shared formula: guessing
    wrong here would mark real pool objects dead, so an upstream a
    snapshot names but the config no longer has is refused rather than
    assumed either way.
    """
    now = now or datetime.now(timezone.utc)
    fetching = store.list_sizes(FETCH_LOCKS)
    if fetching and not force:
        raise GCError(
            f'a fetch is uploading ({", ".join(sorted(fetching))}); pool '
            f'files it uploaded are not referenced until its cut. '
            f'--force if that fetch is gone'
        )
    live = _referenced(store, upstreams)
    listing = _listed(
        POOL_PREFIX, 'all (a full mirror takes minutes)', store.list_objects
    )
    out = Collection(POOL_PREFIX)
    for key, (size, modified) in listing.items():
        if key in live:
            out.kept += 1
        elif now - modified < grace:
            out.young += 1
        else:
            out.dead[key] = size
    return out


def _referenced(store: Store, upstreams: dict[str, Upstream]) -> set[str]:
    """Every _pool/ key a snapshot in _snap/ names

    Each filenames.gz resolves through its own upstream's pool.
    """
    live = set()
    snaps = _listed(SNAP, 'all', store.list_sizes)
    filenames = [k for k in snaps if k.endswith('/filenames.gz')]
    last = time.monotonic()
    for done, key in enumerate(filenames, 1):
        name = key.removeprefix(SNAP).split('/', 1)[0]
        upstream = upstreams.get(name)
        if upstream is None:
            raise GCError(
                f'{key}: upstream {name!r} is not configured (removing '
                f'an upstream loses its pool namespace); retire its '
                f'snapshots first, or restore its aptberg.yaml entry'
            )
        raw = store.get_bytes(key)
        names = gzip.decompress(raw).decode().splitlines()
        live.update(storage_key(upstream, n) for n in names)
        if time.monotonic() - last >= LOG_EVERY:
            last = time.monotonic()
            log.info(
                '%s: read %d/%d filenames.gz, %d live files',
                SNAP,
                done,
                len(filenames),
                len(live),
            )
    log.info(
        '%s: %d files referenced by %d snapshots',
        POOL_PREFIX,
        len(live),
        len(filenames),
    )
    return live


def _listed(
    scope: str, what: str, lister: Callable[[str], dict], prefix: str = ''
) -> dict:
    "lister(prefix or scope), saying what it lists and how long it took"
    start = time.monotonic()
    log.info('%s: listing %s', scope, what)
    found = lister(prefix or scope)
    log.info(
        '%s: %d objects (%.1fs)', scope, len(found), time.monotonic() - start
    )
    return found


def collect(store: Store, collection: Collection) -> int:
    "Delete what a pass found dead; returns the number deleted"
    keys = sorted(collection.dead)
    for i in range(0, len(keys), DELETE_CHUNK):
        store.delete_many(keys[i : i + DELETE_CHUNK])
        if len(keys) > DELETE_CHUNK:
            log.info(
                '%s: deleted %d/%d objects',
                collection.scope,
                min(i + DELETE_CHUNK, len(keys)),
                len(keys),
            )
    if keys:
        log.info('%s: deleted %d objects', collection.scope, len(keys))
    return len(keys)
