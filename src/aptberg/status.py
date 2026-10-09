"""Status: per channel prefix and suite, what is named, served, and since

A cell is one suite of one channel prefix. For each, the manifest's ref,
the ref live now according to _history/, since when, and how old the
served content is upstream. The served bytes are checked against what
the history recorded, so a prefix changed behind aptberg's back shows.

States, worst first:

    drift       the served Release is not the one history recorded
    unrecorded  served, but no history names the suite
    incomplete  the latest event for the prefix was a partial apply
    pending     the manifest names another ref than is served: normal
                right after cut or a manifest edit, until the apply
    behind acc  a prod serving another ref than its acc: normal between
                promotions

The first three are problems; the last two are information.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import Upstream
from .fetch import SIGNATURES
from .history import Live, events, state_at
from .layout import dists as served_dists
from .layout import served_suites
from .manifest import STAGES, Manifest, Ref
from .snapshot import MARKER, snapshot_root
from .store import Store

PROBLEMS = ('drift', 'unrecorded', 'incomplete')


@dataclass
class Cell:
    "One suite of one channel prefix"

    upstream: str
    name: str  # prefix name: acc, stable-prod
    suite: str
    manifest: Ref | None = None
    served: Ref | None = None
    since: datetime | None = None
    release_date: str = ''  # the served snapshot's upstream Date
    states: list[str] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        "The channel prefix, as a key prefix"
        return f'{self.upstream}/ch/{self.name}/'

    @property
    def channel(self) -> str | None:
        "The channel part of the prefix name, if any"
        channel, _, _ = self.name.rpartition('-')
        return channel or None

    @property
    def stage(self) -> str:
        "cur, acc or prod"
        return self.name.rpartition('-')[2]

    @property
    def problem(self) -> bool:
        "Whether any state is a problem rather than information"
        return any(s in PROBLEMS for s in self.states)

    def to_dict(self, now: datetime) -> dict:
        "For --json"
        return {
            'prefix': self.prefix,
            'suite': self.suite,
            'manifest': str(self.manifest) if self.manifest else None,
            'served': str(self.served) if self.served else None,
            'since': self.since.isoformat() if self.since else None,
            'age_seconds': (
                int((now - self.since).total_seconds()) if self.since else None
            ),
            'release_date': self.release_date or None,
            'states': self.states,
        }


def cells(
    store: Store,
    upstream: Upstream,
    root: Path | None,
    bucket: str,
    now: datetime,
    foreign: frozenset[str] = frozenset(),
) -> list[Cell]:
    """Every cell of an upstream, from its manifests and its bucket prefixes

    foreign: suite names owned by another upstream sharing this served
    path (Upstream.path) -- excluded from the auto-discovered set, so
    e.g. debian-old's jessie does not also show up as a cell of debian.
    A suite genuinely orphaned (no upstream's suites: lists it any more)
    still shows, which is the point of discovering from the bucket at
    all, not just from config.
    """
    manifests: dict[str, Manifest] = {}
    if root is not None:
        for manifest in Manifest.every(
            root, bucket, upstream.name, upstream.served
        ):
            manifests[manifest.path.stem] = manifest
    names = set(manifests) | set(store.list_dirs(f'{upstream.served}/ch/'))
    out: list[Cell] = []
    for name in sorted(names, key=_prefix_order):
        out += _prefix_cells(
            store, upstream, name, manifests.get(name), now, foreign
        )
    _mark_behind_acc(out)
    return out


def _prefix_cells(
    store: Store,
    upstream: Upstream,
    name: str,
    manifest: Manifest | None,
    now: datetime,
    foreign: frozenset[str],
) -> list[Cell]:
    "The cells of one channel prefix: what its manifest names or it serves"
    prefix = f'{upstream.served}/ch/{name}/'
    history = events(store, prefix)
    live = state_at(history, now)
    incomplete = bool(history) and not history[-1].complete
    suites = set(live) | set(manifest.suites if manifest else ())
    suites |= set(served_suites(store, [upstream], prefix))
    out = []
    for suite in sorted(suites - foreign):
        cell = Cell(
            upstream.served,
            name,
            suite,
            manifest.suites.get(suite) if manifest else None,
        )
        _served(store, cell, live.get(suite), upstream)
        if incomplete and suite not in history[-1].suites:
            cell.states.append('incomplete')
        if cell.manifest is not None and cell.manifest != cell.served:
            cell.states.append('pending')
        out.append(cell)
    return out


def _mark_behind_acc(cells: list[Cell]) -> None:
    "A prod cell serving another ref than its acc is behind acc"
    served = {(c.name, c.suite): c.served for c in cells}
    for cell in cells:
        if cell.stage == 'prod':
            acc = '-'.join(filter(None, (cell.channel, 'acc')))
            theirs = served.get((acc, cell.suite))
            if theirs is not None and theirs != cell.served:
                cell.states.append('behind acc')


def _served(
    store: Store, cell: Cell, live: Live | None, upstream: Upstream
) -> None:
    dists = served_dists(upstream, cell.prefix, cell.suite)
    actual = next(
        (d for d in (store.sha256(dists + n) for n in SIGNATURES[:2]) if d),
        None,
    )
    if live is None:
        if actual is not None:
            cell.states.append('unrecorded')
        return
    cell.served = live.served.ref
    cell.since = live.since
    if actual != live.served.release_sha256:
        cell.states.append('drift')
    ref = live.served.ref
    raw = store.get_bytes(
        snapshot_root(upstream.name, cell.suite, ref) + MARKER
    )
    if raw is not None:
        cell.release_date = json.loads(raw).get('release_date', '')


def _prefix_order(name: str) -> tuple:
    # verystable-cur, verystable-acc, verystable-prod, ..., cur, acc,
    # prod: STAGES order within a channel, channels before the plain ones
    channel, _, stage = name.rpartition('-')
    return (channel, STAGES.index(stage) if stage in STAGES else 9, name)
