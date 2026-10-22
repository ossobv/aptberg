"""Package-level diff between two snapshots of the same suite

Reads the Packages indexes two already-cut snapshots recorded (from
their snapshot.json entries, not the live filter selection: indexes are
never filtered, only the pool, so this sees exactly what a client would
see) and reports, per component/architecture, which packages were
added, removed, or changed version.

A side is a snapshot ref, or a stage: for each suite, the snapshot that
stage's prefix serves now. That is taken from the prefix's _history/
and checked against the Release it serves, as status does, not from
the manifests: they can name a snapshot not applied yet, and cur's are
not even in the manifests repository, only on the host that cuts. So
diff prod..cur answers "what do cur hosts have that prod hosts do not"
from any host with access to the bucket.

new_files() lists the pool files the second side's indexes name and the
first side's do not, with the hash from the signed index and the path
a client fetches them at: enough to copy an urgent fix into another
repository and check it arrived intact. Only binary packages are
listed; the source files of the same packages (a Sources stanza's .dsc
and tarballs, index.source_files) could be added the same way, for a
repository that wants source too.
"""

import json
import logging
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from . import index
from .config import Upstream
from .deb822 import stanzas
from .fetch import SIGNATURES
from .history import events, state_at
from .layout import dists as served_dists
from .layout import owner
from .manifest import CUT_STAGE, STAGES, Ref, prefix_name
from .promote import Unit
from .release import Entry, family
from .snapshot import MARKER, snapshot_root
from .store import Store

log = logging.getLogger(__name__)

# The Packages fields kept per stanza; the rest is never looked at, and
# a suite's indexes hold hundreds of thousands of stanzas.
FIELDS = (
    'Package',
    'Version',
    'Architecture',
    'Source',
    'Filename',
    'Size',
    'SHA256',
)


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


@dataclass(frozen=True)
class Pair:
    "One suite of one upstream, and the snapshot each side has of it"

    upstream: Upstream
    channel: str | None
    suite: str
    # None: that side does not have the suite
    a: Ref | None
    b: Ref | None
    # The stage whose prefix the paths of new files are under
    stage: str = CUT_STAGE

    @property
    def prefix(self) -> str:
        "The channel prefix path new files are fetched under: ubuntu/ch/cur/"
        name = prefix_name(self.channel, self.stage)
        return f'{self.upstream.served}/ch/{name}/'


@dataclass(frozen=True, order=True)
class PoolFile:
    "A pool file one side's indexes name and the other side's do not"

    codename: str
    component: str
    path: str
    architecture: str
    package: str
    version: str
    source: str
    size: int
    sha256: str

    def to_dict(self) -> dict:
        "The fields, for --json"
        return {
            'codename': self.codename,
            'component': self.component,
            'path': self.path,
            'architecture': self.architecture,
            'package': self.package,
            'version': self.version,
            'source': self.source,
            'size': self.size,
            'sha256': self.sha256,
        }


def sides(text: str) -> tuple[str | Ref, str | Ref]:
    "OLD..NEW: each a stage name or a snapshot reference"
    a, _, b = text.partition('..')
    if text.count('..') != 1 or '...' in text or not a or not b:
        raise DiffError(f'not OLD..NEW: {text!r}')
    return _side(a), _side(b)


def backwards(a: str | Ref, b: str | Ref) -> bool:
    """Whether a range of two stages runs from newer to older

    cur is the newest, prod the oldest: acc..cur is what is coming,
    cur..acc what acc has that cur no longer does. Allowed, since a
    rollback is worth a look too, but rarely what was meant.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return STAGES.index(a) < STAGES.index(b)


def _side(text: str) -> str | Ref:
    return text if text in STAGES else Ref.parse(text)


def pairs(
    store: Store,
    upstreams: dict[str, Upstream],
    units: list[Unit],
    a: str | Ref,
    b: str | Ref,
    suites: list[str] | None = None,
    codenames: list[str] | None = None,
) -> list[Pair]:
    """The suites to compare, and the snapshot each side has of them

    upstreams: every upstream of the config, to tell which suites of a
    shared served path are whose. A stage side takes each suite's
    served ref; a ref side takes the ref for every suite, so it needs
    suites (or, against a stage, codenames) named. Without suites or
    codenames, every suite either stage serves. A suite that both sides
    have at the same ref is left out: there is nothing to read.

    A stage prefix that serves nothing at all is skipped with a warning
    (an upstream not promoted that far yet): comparing against it would
    report every package as added or removed. Finding no suite to
    compare at all, or a suite asked for that no unit has, is an error,
    so neither looks like "no differences".
    """
    stages = [s for s in (b, a) if isinstance(s, str)]
    if not stages and codenames:
        raise DiffError('--codename needs a stage side; name --suite')
    if len(stages) < 2 and not suites and not codenames:
        raise DiffError('a snapshot ref compares the --suite named')
    stage = stages[0] if stages else CUT_STAGE
    out = []
    claimed: set[str] = set()
    for unit in units:
        for upstream in unit.upstreams:
            owners = {
                n: u
                for n, u in upstreams.items()
                if u.served == upstream.served
            }
            refs = []
            for which in (a, b):
                got = _refs(store, upstream, owners, unit.channel, which)
                if isinstance(got, dict) and not got:
                    name = prefix_name(unit.channel, which)
                    log.warning(
                        '%s: %s/ch/%s serves nothing; skipped',
                        upstream.name,
                        upstream.served,
                        name,
                    )
                    break
                refs.append(got)
            else:
                mine = _suites(store, upstream, refs, suites, codenames)
                claimed.update(mine)
                for suite in mine:
                    pa, pb = (_ref(r, suite) for r in refs)
                    if pa != pb:
                        out.append(
                            Pair(upstream, unit.channel, suite, pa, pb, stage)
                        )
    unclaimed = [s for s in suites or () if s not in claimed]
    if unclaimed:
        raise DiffError(f'no such suite: {", ".join(unclaimed)}')
    if not claimed:
        raise DiffError('no suite to compare')
    return out


def _suites(
    store: Store,
    upstream: Upstream,
    refs: list[dict[str, Ref] | Ref],
    suites: list[str] | None,
    codenames: list[str] | None,
) -> list[str]:
    "The suites of upstream to compare, in order"
    have = {
        s
        for r in refs
        for s in (r if isinstance(r, dict) else upstream.suites)
    }
    if not suites and not codenames:
        return sorted(have)
    mine = [s for s in suites or () if s in have]
    if codenames:
        # The codename the snapshot's Release carried, recorded at cut:
        # zabbix noble and kubernetes anydist work as Ubuntu does.
        served = next(r for r in reversed(refs) if isinstance(r, dict))
        for suite, ref in sorted(served.items()):
            raw = store.get_bytes(
                snapshot_root(upstream.name, suite, ref) + MARKER
            )
            codename = json.loads(raw).get('codename') if raw else ''
            if family(codename or suite) in codenames and suite not in mine:
                mine.append(suite)
    return mine


def diff_suite(
    store: Store,
    upstream: str,
    suite: str,
    ref_a: Ref | None,
    ref_b: Ref | None,
    sources: list[str] | None = None,
) -> dict[str, PackageDiff]:
    """Non-empty PackageDiffs by head (e.g. main/binary-amd64), sorted

    A ref of None is a side without the suite: everything the other
    side has is added (or removed). sources keeps only the packages
    built from a source package matching one of these globs.
    """
    packages_a = _versions(_read(store, upstream, suite, ref_a, sources)[1])
    packages_b = _versions(_read(store, upstream, suite, ref_b, sources)[1])
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


def new_files(
    store: Store, pair: Pair, sources: list[str] | None = None
) -> list[PoolFile]:
    """The pool files pair.b's indexes name and pair.a's do not, sorted

    Compared by Filename across the whole suite, so a package that only
    moved between components is not new. An Architecture: all file is
    listed once, not once per binary-<arch> index naming it. The
    codename is the release family of the suite (noble for
    noble-security): the release a copy of the file belongs to.
    sources is as in diff_suite().
    """
    name = pair.upstream.name
    _, old = _read(store, name, pair.suite, pair.a, sources)
    marker, new = _read(store, name, pair.suite, pair.b, sources)
    known = {s['Filename'] for heads in old.values() for s in heads}
    codename = family(marker.get('codename') or pair.suite)
    out = {}
    for head, heads in new.items():
        component = head.rpartition('/')[0]
        for s in heads:
            filename = s.get('Filename')
            if not filename or filename in known or filename in out:
                continue
            out[filename] = PoolFile(
                codename,
                component,
                pair.prefix + _served_name(pair.upstream, filename),
                s.get('Architecture', ''),
                s['Package'],
                s.get('Version', ''),
                source_name(s),
                int(s.get('Size') or 0),
                s.get('SHA256', ''),
            )
    return sorted(out.values())


def source_name(stanza: dict[str, str]) -> str:
    """The source package a binary stanza was built from

    Source: names it, with the source version in parentheses when that
    differs from the binary's ("php8.5 (8.5.4-1)"); without Source:,
    the source has the binary's own name.
    """
    return stanza.get('Source', '').split(' ', 1)[0] or stanza['Package']


def _served_name(upstream: Upstream, filename: str) -> str:
    "Filename relative to the channel prefix it is fetched under"
    return filename.removeprefix('./') if upstream.flat else filename


def _refs(
    store: Store,
    upstream: Upstream,
    owners: dict[str, Upstream],
    channel: str | None,
    which: str | Ref,
) -> dict[str, Ref] | Ref:
    "A side's served ref per suite of upstream (a stage), or its one ref"
    if isinstance(which, Ref):
        return which
    prefix = f'{upstream.served}/ch/{prefix_name(channel, which)}/'
    out = {}
    for suite, live in state_at(events(store, prefix)).items():
        if owner(owners, suite) is not upstream:
            continue
        dists = served_dists(upstream, prefix, suite)
        actual = next(
            (
                d
                for d in (store.sha256(dists + n) for n in SIGNATURES[:2])
                if d
            ),
            None,
        )
        if actual is None:
            # Retired from the prefix since: no longer served.
            continue
        if actual != live.served.release_sha256:
            raise DiffError(
                f'{prefix}{suite}: serves another Release than its history '
                f'records; see aptberg status'
            )
        out[suite] = live.served.ref
    return out


def _ref(refs: dict[str, Ref] | Ref, suite: str) -> Ref | None:
    return refs if isinstance(refs, Ref) else refs.get(suite)


def _read(
    store: Store,
    upstream: str,
    suite: str,
    ref: Ref | None,
    sources: list[str] | None,
) -> tuple[dict, dict[str, list[dict[str, str]]]]:
    "A snapshot's marker and its stanzas by head; nothing for None"
    if ref is None:
        return {}, {}
    root, marker = _snapshot(store, upstream, suite, ref)
    return marker, _packages(store, root, suite, marker, sources)


def _versions(
    packages: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, str]]:
    "Every head's {package name: version}"
    return {
        head: {s['Package']: s.get('Version', '') for s in heads}
        for head, heads in packages.items()
    }


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
    store: Store,
    root: str,
    suite: str,
    marker: dict,
    sources: list[str] | None,
) -> dict[str, list[dict[str, str]]]:
    "Every head's Packages stanzas, only FIELDS, only from sources"
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
            out[head] = [
                {k: s[k] for k in FIELDS if k in s}
                for s in stanzas(fh)
                if 'Package' in s
                and (
                    not sources
                    or any(fnmatchcase(source_name(s), p) for p in sources)
                )
            ]
    return out
