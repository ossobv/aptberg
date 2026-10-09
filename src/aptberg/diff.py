"""Package-level diff between two snapshots of the same suite

Reads the Packages indexes two already-cut snapshots recorded (from
their snapshot.json entries, not the live filter selection: indexes are
never filtered, only the pool, so this sees exactly what a client would
see) and reports, per component/architecture, which packages were
added, removed, or changed version.
"""

import json
from dataclasses import dataclass, field

from . import index
from .deb822 import stanzas
from .manifest import Ref
from .release import Entry
from .snapshot import MARKER, snapshot_root
from .store import Store


class DiffError(Exception):
    "A snapshot could not be diffed"


@dataclass
class PackageDiff:
    "One component/architecture's differences between two snapshots"

    added: dict[str, str] = field(default_factory=dict)
    removed: dict[str, str] = field(default_factory=dict)
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def diff_suite(
    store: Store, upstream: str, suite: str, ref_a: Ref, ref_b: Ref
) -> dict[str, PackageDiff]:
    "Non-empty PackageDiffs by head (e.g. main/binary-amd64), sorted"
    root_a, marker_a = _snapshot(store, upstream, suite, ref_a)
    root_b, marker_b = _snapshot(store, upstream, suite, ref_b)
    packages_a = _packages(store, root_a, suite, marker_a)
    packages_b = _packages(store, root_b, suite, marker_b)
    out = {}
    for head in sorted(set(packages_a) | set(packages_b)):
        a = packages_a.get(head, {})
        b = packages_b.get(head, {})
        d = PackageDiff()
        for name in sorted(set(a) | set(b)):
            va, vb = a.get(name), b.get(name)
            if va == vb:
                continue
            elif va is None:
                d.added[name] = vb
            elif vb is None:
                d.removed[name] = va
            else:
                d.changed[name] = (va, vb)
        if d:
            out[head] = d
    return out


def _snapshot(
    store: Store, upstream: str, suite: str, ref: Ref
) -> tuple[str, dict]:
    "A snapshot's root key prefix and its parsed marker"
    root = snapshot_root(upstream, suite, ref)
    raw = store.get_bytes(root + MARKER)
    if raw is None:
        raise DiffError(f'no such snapshot: {upstream} {suite} {ref}')
    return root, json.loads(raw)


def _packages(
    store: Store, root: str, suite: str, marker: dict
) -> dict[str, dict[str, str]]:
    "Every head's Packages, as {package name: version}"
    entries = [
        Entry(path, sha256, size) for path, sha256, size in marker['entries']
    ]
    out = {}
    for head, entry in index.packages_indexes(entries).items():
        key = f'{root}dists/{suite}/{entry.path}'
        data = store.get_bytes(key)
        if data is None:
            raise DiffError(f'{key}: missing')
        with index.open_text(entry.path, data) as fh:
            out[head] = {
                s['Package']: s['Version']
                for s in stanzas(fh)
                if 'Package' in s
            }
    return out
