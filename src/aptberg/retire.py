"""Retire: drop snapshots from _snap/, which is what frees pool space

gc keeps every .deb any snapshot in _snap/ names, so retiring snapshots
is the retention knob. A snapshot may not be retired while

- a manifest in the checkout names it (it could be applied again), or
- a channel prefix of its upstream served it within --grace (clients
  may still resolve its by-hash objects, and gc needs its entries to
  keep them)

The --unused policy additionally keeps the newest complete snapshot of
every suite (cut compares against it to reuse an id, and it is the
obvious rollback target) and anything cut more recently than
--older-than. Snapshots left incomplete by an interrupted cut qualify once old.

Deletion removes snapshot.json first: a snapshot half way through being
retired is already unusable to apply, never silently incomplete.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Upstream
from .duration import format_duration
from .gc import served_within
from .manifest import Manifest, Ref
from .snapshot import MARKER, SNAP
from .store import Store


class RetireError(Exception):
    "The snapshot cannot be retired"


@dataclass
class Snapshot:
    "One snapshot tree in _snap/"

    upstream: str
    suite: str
    ref: Ref
    root: str
    keys: list[str] = field(default_factory=list)
    size: int = 0
    complete: bool = False
    cut_at: datetime | None = None  # None: unknown (incomplete)
    modified: datetime | None = None  # newest object in the tree

    @property
    def age_from(self) -> datetime | None:
        "When the snapshot came to be, as best known"
        return self.cut_at or self.modified


def snapshots(store: Store, upstream: Upstream) -> list[Snapshot]:
    "Every snapshot tree of an upstream, complete or not"
    prefix = f'{SNAP}{upstream.name}/'
    depth = 3 if upstream.channels else 2  # [tree/]suite/id
    found: dict[str, Snapshot] = {}
    for key, (size, modified) in store.list_objects(prefix).items():
        parts = key[len(prefix) :].split('/')
        if len(parts) <= depth:
            continue
        tree = parts[0] if upstream.channels else None
        suite, snap_id = parts[depth - 2], parts[depth - 1]
        root = prefix + '/'.join(parts[:depth]) + '/'
        snap = found.get(root)
        if snap is None:
            snap = found[root] = Snapshot(
                upstream.name, suite, Ref(tree, snap_id), root
            )
        snap.keys.append(key)
        snap.size += size
        if snap.modified is None or modified > snap.modified:
            snap.modified = modified
        if key == root + MARKER:
            snap.complete = True
    for snap in found.values():
        if snap.complete:
            raw = json.loads(store.get_bytes(snap.root + MARKER))
            snap.cut_at = datetime.fromisoformat(raw['cut_at'])
    return sorted(
        found.values(), key=lambda s: (s.suite, str(s.ref.tree), s.ref.id)
    )


def blockers(
    snap: Snapshot,
    manifests: list[Manifest],
    served: dict[str, dict[str, set[Ref]]],
) -> list[str]:
    "Why snap may not be retired; empty if it may"
    out = []
    for manifest in manifests:
        if manifest.suites.get(snap.suite) == snap.ref:
            out.append(f'named by {manifest.path}')
    for prefix, suites in sorted(served.items()):
        if snap.ref in suites.get(snap.suite, ()):
            out.append(f'served by {prefix} within --grace')
    return out


def policy(
    snaps: list[Snapshot], older_than: timedelta, now: datetime
) -> dict[str, str]:
    "root -> why the --unused policy keeps it, beyond hard blockers"
    out = {}
    newest: dict[tuple, Snapshot] = {}
    for snap in snaps:
        if snap.complete:
            key = (snap.suite, snap.ref.tree)
            if key not in newest or snap.ref.id > newest[key].ref.id:
                newest[key] = snap
    for snap in newest.values():
        out[snap.root] = 'newest of its suite'
    for snap in snaps:
        if (
            snap.root not in out
            and snap.age_from
            and (now - snap.age_from < older_than)
        ):
            out[snap.root] = f'cut less than {format_duration(older_than)} ago'
    return out


def context(
    store: Store,
    upstream: Upstream,
    root: Path | None,
    bucket: str,
    grace: timedelta,
    now: datetime,
) -> tuple[list[Manifest], dict[str, dict[str, set[Ref]]]]:
    "Every manifest of the upstream, and what its prefixes served lately"
    if root is None:
        raise RetireError(
            'retire needs manifests: in config, to know '
            'which snapshots are still named'
        )
    manifests = Manifest.every(root, bucket, upstream.name, upstream.served)
    served = {}
    for name in store.list_dirs(f'{upstream.served}/ch/'):
        prefix = f'{upstream.served}/ch/{name}/'
        served[prefix] = served_within(store, prefix, grace, now)
    return manifests, served


def retire(store: Store, snap: Snapshot) -> int:
    "Delete a snapshot tree, its marker first; returns objects deleted"
    keys = sorted(snap.keys)
    marker = snap.root + MARKER
    if marker in keys:
        store.delete(marker)
        keys.remove(marker)
    store.delete_many(keys)
    return len(keys) + (1 if snap.complete else 0)


def now_utc() -> datetime:
    "The current time, UTC"
    return datetime.now(timezone.utc)
