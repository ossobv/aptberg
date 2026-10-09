"""Promote: acc -> prod, per channel prefix; with --apply, apply prod

prod only ever receives a snapshot that acc serves or has served. The
acc manifest is not proof of that: cut writes it before anyone applies
acc. So by default every promoted suite is checked against what the acc
prefix actually serves (its Release files carry the snapshot's sha256),
and a suite acc does not serve yet refuses its unit (below). With an
explicit ref (one suite), acc's _history/ must show it served that ref
at some point: promoting the well-soaked snapshot acc has since moved
past.

Promotion is per channel prefix and optionally per suite: noble and
jammy are separate decisions with separate soaks. A group (ubuntu and
ubuntu-security; kubernetes and etcd) is promoted together: every
member is prepared before anything is written, and the suites asked for
go to whichever members have them, so promoting only noble-security
still touches only ubuntu-security.

Everything promote moves splits into units: one group on one channel
(Unit). Units are independent of each other: one that is refused does
not hold back the rest.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Upstream
from .fetch import SIGNATURES
from .history import events, served_ever
from .layout import dists as served_dists
from .manifest import Manifest, Ref
from .release import family
from .snapshot import MARKER, snapshot_root
from .store import Store


class PromoteError(Exception):
    "The promotion is not allowed"


@dataclass
class Promotion:
    "The prod manifest as it will be, and what changes in it"

    acc: Manifest
    prod: Manifest
    # suite -> (ref prod had, or None; ref it gets)
    changes: dict[str, tuple[Ref | None, Ref]] = field(default_factory=dict)
    # suite -> why acc counts as having held it
    evidence: dict[str, str] = field(default_factory=dict)

    def lines(self) -> list[str]:
        "Human-readable changes"
        if not self.changes:
            return [f'{self.prod.path}: nothing to promote']
        return [f'{self.prod.path}:'] + [
            f'  {suite}: {old or "(none)"} -> {new} ({self.evidence[suite]})'
            for suite, (old, new) in sorted(self.changes.items())
        ]


@dataclass
class Unit:
    "Upstreams promoted together: a group's members on one channel"

    upstreams: list[Upstream]
    channel: str | None

    def __str__(self) -> str:
        names = ', '.join(u.name for u in self.upstreams)
        return f'{names} {self.channel}' if self.channel else names


def units(
    upstreams: list[Upstream], channels: list[str] | None = None
) -> list[Unit]:
    """The units of these upstreams, in their order

    A group's members without channels form one unit, its members with
    channels one per channel. channels keeps only those channels, and
    so only upstreams with channels; one that none of them has is an
    error.
    """
    unknown = [
        c
        for c in channels or ()
        if not any(c in u.channels for u in upstreams)
    ]
    if unknown:
        raise PromoteError(
            f'no channel {", ".join(unknown)} in '
            f'{", ".join(u.name for u in upstreams)}'
        )
    groups: dict[str, list[Upstream]] = {}
    for upstream in upstreams:
        groups.setdefault(upstream.unit, []).append(upstream)
    out = []
    for members in groups.values():
        plain = [u for u in members if not u.channels]
        if plain and not channels:
            out.append(Unit(plain, None))
        names = dict.fromkeys(c for u in members for c in u.channels)
        for channel in names:
            if not channels or channel in channels:
                out.append(
                    Unit(
                        [u for u in members if channel in u.channels], channel
                    )
                )
    return out


def acc_suites(root: Path, bucket: str, unit: Unit) -> set[str]:
    "Every suite the acc manifests of a unit name"
    return {
        suite
        for upstream in unit.upstreams
        for suite in _manifest(
            root, bucket, upstream, unit.channel, 'acc'
        ).suites
    }


def prepare(
    root: Path,
    bucket: str,
    upstream: Upstream,
    channel: str | None,
    suites: list[str] | None,
    store: Store,
    ref: Ref | None = None,
) -> Promotion:
    """Work out a promotion without writing anything

    suites defaults to every suite of the acc manifest. ref promotes that
    snapshot instead of acc's current one, for exactly one suite.
    """
    if ref is not None and (not suites or len(suites) != 1):
        raise PromoteError('a ref promotes exactly one --suite')
    _check_channel(upstream, channel)
    acc = _manifest(root, bucket, upstream, channel, 'acc')
    if not acc.path.exists():
        raise PromoteError(
            f'{acc.path}: no such manifest; nothing was cut for it'
        )
    prod = _manifest(root, bucket, upstream, channel, 'prod')
    chosen = suites or sorted(acc.suites)
    unknown = [s for s in chosen if s not in acc.suites]
    if unknown:
        raise PromoteError(f'{acc.path}: no suite {", ".join(unknown)}')

    out = Promotion(acc, prod)
    for suite in chosen:
        if ref is None:
            new = acc.suites[suite]
            _check_served(store, acc, upstream, suite, new)
            why = 'acc serves it'
        else:
            new = ref
            event = served_ever(events(store, acc.key_prefix), suite, new)
            if event is None:
                raise PromoteError(
                    f'{acc.prefix} never served {suite} {new} according '
                    f'to its history'
                )
            why = f'acc served it from {event.at:%Y-%m-%d %H:%M} UTC'
        if prod.suites.get(suite) != new:
            out.changes[suite] = (prod.suites.get(suite), new)
            out.evidence[suite] = why
            prod.suites[suite] = new
    return out


def prepare_group(
    root: Path,
    bucket: str,
    upstreams: list[Upstream],
    channel: str | None,
    suites: list[str] | None,
    store: Store,
    ref: Ref | None = None,
) -> list[Promotion]:
    """prepare() for every upstream of a group, or nothing at all

    Without suites every member promotes all its acc suites. With
    suites, each member promotes those it has; a member with none of
    them is left out, and a suite no member has is an error.
    """
    if len(upstreams) == 1:
        return [
            prepare(root, bucket, upstreams[0], channel, suites, store, ref)
        ]
    if ref is not None and (not suites or len(suites) != 1):
        raise PromoteError('a ref promotes exactly one --suite')
    for upstream in upstreams:
        _check_channel(upstream, channel)
    out = []
    claimed: set[str] = set()
    for upstream in upstreams:
        mine = suites
        if suites:
            acc = _manifest(root, bucket, upstream, channel, 'acc')
            mine = [s for s in suites if s in acc.suites]
            if not mine:
                continue
            claimed.update(mine)
        out.append(prepare(root, bucket, upstream, channel, mine, store, ref))
    unclaimed = [s for s in (suites or ()) if s not in claimed]
    if unclaimed:
        raise PromoteError(
            f'no acc manifest of {", ".join(u.name for u in upstreams)} '
            f'has suite {", ".join(unclaimed)}'
        )
    return out


@dataclass
class Prepared:
    "Every unit's promotions, worked out; nothing written yet"

    groups: list[list[Promotion]] = field(default_factory=list)
    refused: list[tuple[Unit, str]] = field(default_factory=list)
    unclaimed: list[str] = field(default_factory=list)  # asked, nowhere


def prepare_units(
    root: Path,
    bucket: str,
    units: list[Unit],
    store: Store,
    suites: list[str] | None = None,
    codenames: list[str] | None = None,
    ref: Ref | None = None,
) -> Prepared:
    """prepare_group() for every unit; one refused does not stop the rest

    suites and codenames name suites across every unit: each unit takes
    those it has, which may be none, and then is left out. What no unit
    has is unclaimed.
    """
    out = Prepared()
    claimed: set[str] = set()
    for unit in units:
        try:
            mine = _unit_suites(root, bucket, unit, store, suites, codenames)
            if mine == []:
                continue
            claimed.update(mine or ())
            out.groups.append(
                prepare_group(
                    root,
                    bucket,
                    unit.upstreams,
                    unit.channel,
                    mine,
                    store,
                    ref,
                )
            )
        except PromoteError as exc:
            out.refused.append((unit, str(exc)))
    out.unclaimed = [s for s in suites or () if s not in claimed]
    if codenames and not claimed:
        out.unclaimed += codenames
    return out


def _unit_suites(
    root: Path,
    bucket: str,
    unit: Unit,
    store: Store,
    suites: list[str] | None,
    codenames: list[str] | None,
) -> list[str] | None:
    "The suites asked for that this unit has; None if none were asked for"
    if not suites and not codenames:
        return None
    have = acc_suites(root, bucket, unit)
    mine = [s for s in suites or () if s in have]
    if codenames:
        found = codename_suites(
            root,
            bucket,
            unit.upstreams,
            unit.channel,
            codenames,
            store,
            strict=False,
        )
        mine += [s for s in found if s not in mine]
    return mine


def codename_suites(
    root: Path,
    bucket: str,
    upstreams: list[Upstream],
    channel: str | None,
    codenames: list[str],
    store: Store,
    strict: bool = True,
) -> list[str]:
    """The acc suites of the group whose snapshot has one of codenames

    The codename is the one the snapshot's Release carried, recorded in
    its snapshot.json at cut, not guessed from the suite name: zabbix
    noble and kubernetes anydist work the same way as Ubuntu. strict
    refuses a codename no suite has.
    """
    for upstream in upstreams:
        _check_channel(upstream, channel)
    found: dict[str, list[str]] = {c: [] for c in codenames}
    for upstream in upstreams:
        acc = _manifest(root, bucket, upstream, channel, 'acc')
        for suite, ref in sorted(acc.suites.items()):
            raw = store.get_bytes(
                snapshot_root(upstream.name, suite, ref) + MARKER
            )
            codename = json.loads(raw).get('codename') if raw else ''
            if family(codename or suite) in found:
                found[family(codename or suite)].append(suite)
    unmatched = [c for c, suites in found.items() if not suites]
    if unmatched and strict:
        raise PromoteError(
            f'no acc suite of {", ".join(u.name for u in upstreams)} has '
            f'codename {", ".join(unmatched)}'
        )
    return sorted({s for suites in found.values() for s in suites})


def _manifest(
    root: Path,
    bucket: str,
    upstream: Upstream,
    channel: str | None,
    stage: str,
) -> Manifest:
    "The upstream's manifest of this channel and stage"
    return Manifest.load(
        root, bucket, upstream.name, upstream.served, channel, stage
    )


def _check_channel(upstream: Upstream, channel: str | None) -> None:
    if upstream.channels and channel is None:
        raise PromoteError(
            f'{upstream.name} has channels; name one of '
            f'{", ".join(sorted(upstream.channels))}'
        )
    if not upstream.channels and channel is not None:
        raise PromoteError(f'{upstream.name} has no channels')
    if channel is not None and channel not in upstream.channels:
        raise PromoteError(f'{upstream.name} has no channel {channel!r}')


def _check_served(
    store: Store, acc: Manifest, upstream: Upstream, suite: str, ref: Ref
) -> None:
    root = snapshot_root(upstream.name, suite, ref)
    served = served_dists(upstream, acc.key_prefix, suite)
    checked = False
    for name in SIGNATURES:
        want = store.sha256(f'{root}dists/{suite}/{name}')
        if want is None:
            continue
        have = store.sha256(served + name)
        if have != want:
            raise PromoteError(
                f'{acc.prefix} does not serve {suite} {ref} ({name} '
                f'differs); apply acc first'
            )
        checked = True
    if not checked:
        raise PromoteError(f'{root}: no Release files; not a snapshot')
