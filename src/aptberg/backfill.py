"""Backfill: pool files the current filter wants for existing snapshots

A widened filter only reaches new cuts. Snapshots already served or
named by a manifest were cut under the old filter: their indexes name
packages the filter now wants but that were never fetched, which is a
404 until the next cut, and forever for a rollback target.

Backfill loads those snapshots' Packages and Sources indexes from
_snap/ instead of upstream, verified the same way apply verifies a
snapshot (signature, Release, snapshot.json, then each index's
sha256), and hands them to the same selection and pool upload fetch
uses. The pool files come from the snapshot's own source URL, so a
file upstream has since deleted fails, and is reported. selected.gz of
a snapshot stays as cut.

This only ever reads _snap/, never upstream, for the indexes
themselves -- a snapshot's marker["entries"] is exactly what its own
cut fetched, fixed forever. So this widens which *names* pool files
get pulled for, within an index kind a snapshot already has; it cannot
add an index kind (Sources, say) a snapshot never fetched in the first
place, because there is nothing under _snap/ to read for it. A suite
whose live snapshot predates deb_src being turned on needs an ordinary
fetch/sync + cut, not this: the Release being unchanged does not reuse
the old snapshot id, since more files are now being taken (see
snapshot._same); it cuts a fresh one with the same package
versions, this time with source too.
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path

from . import index
from .config import Config, Source, Upstream
from .fetch import FetchedSuite
from .history import events, state_at
from .manifest import Manifest, Ref
from .plan import PlanError, verified_snapshot
from .release import Entry
from .retire import snapshots
from .snapshot import snapshot_root
from .store import Store

log = logging.getLogger(__name__)


def live_refs(
    store: Store, cfg: Config, now: datetime
) -> dict[tuple[str, str], set[Ref]]:
    """(upstream, suite) -> every ref a manifest names or a prefix serves

    Manifests of upstreams no longer configured are skipped. A served
    prefix may be shared with another upstream (Upstream.path), so a
    suite found there only counts if this upstream's own suites: name
    it -- not every suite the prefix happens to serve.
    """
    out: dict[tuple[str, str], set[Ref]] = {}
    for name, upstream in cfg.upstreams.items():
        if cfg.manifests is not None:
            for manifest in Manifest.every(
                cfg.manifests, cfg.bucket, name, upstream.served
            ):
                for suite, ref in manifest.suites.items():
                    out.setdefault((name, suite), set()).add(ref)
        for prefix_name in store.list_dirs(f'{upstream.served}/ch/'):
            live = state_at(
                events(store, f'{upstream.served}/ch/{prefix_name}/'), now
            )
            for suite, entry in live.items():
                if suite in upstream.suites:
                    out.setdefault((name, suite), set()).add(entry.served.ref)
    return out


def all_refs(store: Store, cfg: Config) -> dict[tuple[str, str], set[Ref]]:
    "(upstream, suite) -> every complete snapshot in _snap/"
    out: dict[tuple[str, str], set[Ref]] = {}
    for upstream in cfg.upstreams.values():
        for snap in snapshots(store, upstream):
            if snap.complete:
                out.setdefault((upstream.name, snap.suite), set()).add(
                    snap.ref
                )
    return out


def load(
    store: Store,
    cfg: Config,
    refs: dict[tuple[str, str], set[Ref]],
    workdir: Path,
    override_source_url: bool = False,
) -> list[FetchedSuite]:
    """The snapshots' Packages and Sources indexes, verified, as if fetched

    Pool files download from each snapshot's own recorded source URL by
    default (frozen at cut time, in snapshot.json -- never rewritten,
    since a snapshot is written once). override_source_url instead uses
    each upstream's *current* url: (or channel url, for the ref's own
    tree) from this config -- e.g. the original host is down or gone;
    point url: at a mirror first, then backfill with this. Either way
    every pool file is still verified against the hash and size the
    signed Release named, which is independent of where the bytes came
    from, and this always resolves per upstream, from that upstream's
    own config, so it can never apply one upstream's replacement host
    to another's files.
    """
    out = []
    for (name, suite), suite_refs in sorted(refs.items()):
        upstream = cfg.upstreams[name]
        for ref in sorted(suite_refs):
            root = snapshot_root(name, suite, ref)
            marker, _, release = verified_snapshot(
                store, root, upstream, suite
            )
            entries = [
                Entry(p, sha, size) for p, sha, size in marker['entries']
            ]
            wanted = list(index.packages_indexes(entries).values()) + list(
                index.sources_indexes(entries).values()
            )
            dest = workdir / name / (ref.tree or '') / suite / ref.id
            _copy_indexes(store, f'{root}dists/{suite}/', wanted, dest)
            if override_source_url:
                fs_source = _current_source(upstream, ref)
            else:
                fs_source = Source(name, ref.tree, marker['source'])
            out.append(
                FetchedSuite(
                    upstream,
                    fs_source,
                    suite,
                    release,
                    dest,
                    tuple(wanted),
                    marker['release_sha256'],
                )
            )
            log.info('%s: loaded %d indexes', root, len(wanted))
    return out


def _copy_indexes(
    store: Store, dists: str, entries: list[Entry], dest: Path
) -> None:
    "Copy a snapshot's indexes to dest, each as its Release names it"
    for entry in entries:
        data = store.get_bytes(dists + entry.path)
        if (
            data is None
            or len(data) != entry.size
            or (hashlib.sha256(data).hexdigest() != entry.sha256)
        ):
            raise PlanError(
                f'{dists}{entry.path}: missing or not as the Release says'
            )
        target = dest / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _current_source(upstream: Upstream, ref: Ref) -> Source:
    "The config's source today for the ref's tree"
    for source in upstream.sources():
        if source.tree == ref.tree:
            return source
    raise PlanError(
        f'{upstream.name}: no current source for tree {ref.tree!r}; '
        f'the config no longer has it'
    )
