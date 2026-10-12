"""Plan: what apply must do to make a channel prefix serve a manifest

A plan is a flat list of ops, each in one of three phases, applied in
order with a barrier in between:

    byhash   index files to dists/<suite>/<dir>/by-hash/SHA256/<sha>:
             new keys only, nothing overwritten
    index    index files to their canonical paths (overwrites; read
             only by clients with by-hash disabled)
    release  InRelease, Release, Release.gpg: the flip, suite by suite.
             Signature files the snapshot lacks are deleted, or clients
             would keep reading the previous signed state

A client holding the old InRelease still resolves its old by-hash keys,
because nothing before the release phase removes anything. Everything is
diffed against the prefix first, so only missing or changed objects
appear in the plan.

Before anything is planned, each snapshot's signature is verified again
from _snap/ against the upstream keyring, and its snapshot.json is
checked against the signed Release: nothing unverified reaches a served
prefix.

The pool is not listed. A snapshot exists only once its cut found every
.deb it selected in _pool/, and gc keeps every file a snapshot in _snap/
names until that snapshot is retired: the pool cannot have lost them
since. That makes applying a snapshot -- and promoting it, which is the
same thing for another prefix -- cost what the snapshot's dists/ cost,
not a listing of millions of pool keys. The one exception is a snapshot
cut with --allow-missing: the files its snapshot.json records as
missing are looked up one by one (backfill may have brought them since)
and reported. A full check of the pool against the snapshots a prefix
serves is what verify is for.
"""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Literal

from . import index
from .config import Upstream
from .fetch import SIGNATURES
from .layout import dists as served_dists
from .layout import listing_root
from .manifest import Manifest, Ref
from .pool import storage_key
from .progress import human
from .release import Release, SignatureError, verified_release
from .snapshot import MARKER, snapshot_root
from .store import Store

log = logging.getLogger(__name__)

PHASES = ('byhash', 'index', 'release')


class PlanError(Exception):
    "The manifest cannot be applied"


@dataclass(frozen=True, slots=True)
class Op:
    "One object operation"

    phase: str
    kind: Literal['copy', 'delete']
    dst: str
    src: str | None = None
    size: int = 0
    sha256: str | None = None
    suite: str = ''


@dataclass
class Plan:
    "Everything apply will do to one channel prefix"

    prefix: str
    suites: dict[str, Ref]
    ops: list[Op] = field(default_factory=list)
    # suite -> {"InRelease": sha256, ...} of the snapshot it will serve
    signatures: dict[str, dict[str, str]] = field(default_factory=dict)
    # pool files a snapshot was cut without, still not in _pool/
    pool_accepted_missing: list[str] = field(default_factory=list)

    def by_phase(self, phase: str) -> list[Op]:
        "The ops of one phase, in plan order"
        return [op for op in self.ops if op.phase == phase]

    def summary(self) -> list[str]:
        "Human-readable lines, one per phase"
        lines = [
            f'{self.prefix}: '
            + ', '.join(f'{s} {r}' for s, r in sorted(self.suites.items()))
        ]
        if self.pool_accepted_missing:
            lines.append(
                f'  pool     {len(self.pool_accepted_missing)} files missing '
                f'(accepted at cut)'
            )
        for phase in PHASES:
            ops = self.by_phase(phase)
            copies = [op for op in ops if op.kind == 'copy']
            deletes = len(ops) - len(copies)
            lines.append(
                f'  {phase:<8} {len(copies)} copies, '
                f'{human(sum(op.size for op in copies))}'
                + (f', {deletes} deletes' if deletes else '')
            )
        return lines


def build(
    manifest: Manifest,
    upstream: Upstream,
    store: Store,
    concurrency: int = 8,
) -> Plan:
    "Diff the manifest against its prefix; verify what it points at"
    if manifest.upstream != upstream.name:
        raise PlanError(f'{manifest.path} is not for {upstream.name}')
    prefix = manifest.key_prefix
    plan = Plan(prefix, dict(manifest.suites))
    if not manifest.suites:
        return plan
    present = store.list_sizes(listing_root([upstream], prefix))
    builder = _Builder(plan, upstream, store, present, concurrency)
    for suite, ref in sorted(manifest.suites.items()):
        if (ref.tree is None) != (not upstream.channels):
            raise PlanError(
                f'{manifest.path}: {suite}: {ref} does not fit '
                f'{upstream.name}, which '
                f'{"has" if upstream.channels else "has no"} channels'
            )
        builder.add_suite(suite, ref)
    builder.add_overwrites()
    plan.ops.sort(key=_order)
    return plan


class _Builder:
    """The ops of one plan, suite by suite

    New by-hash objects are copied outright. Canonical indexes and
    signature files overwrite, so they are collected first and copied
    only where what is there differs (add_overwrites).
    """

    def __init__(
        self,
        plan: Plan,
        upstream: Upstream,
        store: Store,
        present: dict[str, int],
        concurrency: int,
    ) -> None:
        self.plan = plan
        self.upstream = upstream
        self.store = store
        self.present = present
        self.concurrency = concurrency
        # (phase, dst, src, sha256, size, suite) of objects that overwrite
        self.overwrites: list[tuple[str, str, str, str, int, str]] = []

    def add_suite(self, suite: str, ref: Ref) -> None:
        "Verify a suite's snapshot, and add what serving it takes"
        plan, upstream = self.plan, self.upstream
        root = snapshot_root(upstream.name, suite, ref)
        marker, signatures, _ = verified_snapshot(
            self.store, root, upstream, suite
        )
        plan.signatures[suite] = {n: sha for n, (sha, _) in signatures.items()}
        plan.pool_accepted_missing += _still_missing(
            self.store, marker, upstream, self.concurrency
        )
        src = f'{root}dists/{suite}/'
        dst = served_dists(upstream, plan.prefix, suite)
        for path, sha256, size in marker['entries']:
            byhash = f'{dst}{index.by_hash_dir(path)}/{sha256}'
            if byhash not in self.present:
                plan.ops.append(
                    Op(
                        'byhash',
                        'copy',
                        byhash,
                        src + path,
                        size,
                        sha256,
                        suite,
                    )
                )
            self.overwrites.append(
                ('index', dst + path, src + path, sha256, size, suite)
            )
        for name in SIGNATURES:
            if name in signatures:
                sha256, size = signatures[name]
                self.overwrites.append(
                    ('release', dst + name, src + name, sha256, size, suite)
                )
            elif dst + name in self.present:
                plan.ops.append(
                    Op('release', 'delete', dst + name, suite=suite)
                )

    def add_overwrites(self) -> None:
        "Copy the overwriting objects whose content differs"
        have = _sha256s(
            self.store,
            [w[1] for w in self.overwrites if w[1] in self.present],
            self.concurrency,
        )
        for phase, dst, src, sha256, size, suite in self.overwrites:
            if have.get(dst) != sha256:
                self.plan.ops.append(
                    Op(phase, 'copy', dst, src, size, sha256, suite)
                )


def _order(op: Op) -> tuple:
    # The release phase goes suite by suite: InRelease, Release,
    # Release.gpg, then deletes. A suite is live once its group is done,
    # which is what history records after a partial failure.
    if op.phase != 'release':
        return (PHASES.index(op.phase), '', False, 0, op.dst)
    name = op.dst.rpartition('/')[2]
    return (
        PHASES.index(op.phase),
        op.suite,
        op.kind == 'delete',
        SIGNATURES.index(name),
        op.dst,
    )


def verified_snapshot(
    store: Store, root: str, upstream: Upstream, suite: str
) -> tuple[dict, dict[str, tuple[str, int]], Release]:
    """A snapshot's marker, its signature files' sha256 and size, and
    its Release

    Fails unless the signature verifies against the upstream keyring and
    every entry of the marker is exactly as the signed Release says.
    """
    raw = store.get_bytes(root + MARKER)
    if raw is None:
        raise PlanError(
            f'{root}: no {MARKER}; snapshot missing or its cut did not finish'
        )
    marker = json.loads(raw)
    if marker.get('suite') != suite:
        raise PlanError(f'{root}{MARKER}: is for {marker.get("suite")}')
    dists = f'{root}dists/{suite}/'
    files = {}
    signatures = {}
    for name in marker['signatures']:
        data = store.get_bytes(dists + name)
        if data is None:
            raise PlanError(f'{dists}{name}: missing')
        files[name] = data
        signatures[name] = (hashlib.sha256(data).hexdigest(), len(data))
    try:
        payload = verified_release(files, upstream.keyring)
    except SignatureError as exc:
        raise SignatureError(f'{dists}: {exc}') from exc
    if (
        hashlib.sha256(payload.encode()).hexdigest()
        != (marker['release_sha256'])
    ):
        raise PlanError(f'{dists}: Release does not match {MARKER}')
    release = Release.parse(payload)
    for path, sha256, size in marker['entries']:
        entry = release.entries.get(path)
        if entry is None or (entry.sha256, entry.size) != (sha256, size):
            raise PlanError(f'{dists}{path}: not as the Release says')
    return marker, signatures, release


def _still_missing(
    store: Store, marker: dict, upstream: Upstream, concurrency: int
) -> list[str]:
    "The pool files a snapshot was cut without that are still not there"
    names = marker.get('missing', ())
    keys = [storage_key(upstream, n) for n in names]
    have = _sha256s(store, keys, concurrency)
    return [n for n, k in zip(names, keys, strict=True) if have[k] is None]


def _sha256s(
    store: Store, keys: list[str], concurrency: int
) -> dict[str, str | None]:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return dict(zip(keys, pool.map(store.sha256, keys), strict=True))
