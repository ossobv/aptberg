"""History: an append-only record of what each channel prefix served

    _history/<upstream>/ch/<prefix>/<UTC time>-<host>-<pid>.json

apply writes one event whenever it changed what a prefix serves, after
its release phase. An event lists every suite the prefix then served,
with the snapshot ref and the sha256 of the InRelease (or Release) that
went out, so "what was live when" can be answered and checked against
the snapshot's own bytes. An apply that failed part-way through its
release phase records the suites that did go live, marked incomplete.

Events are separate objects, never rewritten: nothing reads, modifies
and writes. Key names sort chronologically. For the record to count as
proof, the bucket should keep _history/ from being altered (versioning
or object lock); aptberg itself never deletes or rewrites it.
"""

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Upstream
from .layout import owner
from .manifest import Ref
from .snapshot import snapshot_root
from .store import Store

HISTORY = '_history/'


class HistoryError(Exception):
    "The history cannot answer the question"


@dataclass(frozen=True)
class Served:
    "One suite as a prefix served it"

    ref: Ref
    release_sha256: str  # of InRelease, else of Release


@dataclass
class Event:
    "What a prefix served after one apply"

    at: datetime
    prefix: str
    suites: dict[str, Served]
    complete: bool = True
    note: str = ''
    host: str = ''
    pid: int = 0
    key: str = ''

    def to_json(self) -> bytes:
        "The stored form"
        return (
            json.dumps(
                {
                    'at': self.at.isoformat(timespec='microseconds'),
                    'prefix': self.prefix,
                    'complete': self.complete,
                    'note': self.note,
                    'host': self.host,
                    'pid': self.pid,
                    'suites': {
                        s: {
                            'ref': str(v.ref),
                            'release_sha256': v.release_sha256,
                        }
                        for s, v in sorted(self.suites.items())
                    },
                },
                indent=1,
            ).encode()
            + b'\n'
        )

    @classmethod
    def from_json(cls, data: bytes, key: str = '') -> 'Event':
        "Parse the stored form"
        raw = json.loads(data)
        return cls(
            at=datetime.fromisoformat(raw['at']),
            prefix=raw['prefix'],
            suites={
                s: Served(Ref.parse(v['ref']), v['release_sha256'])
                for s, v in raw['suites'].items()
            },
            complete=raw.get('complete', True),
            note=raw.get('note', ''),
            host=raw.get('host', ''),
            pid=raw.get('pid', 0),
            key=key,
        )


def history_prefix(prefix: str) -> str:
    "Where the events of a channel prefix (ubuntu/ch/acc/) live"
    return HISTORY + prefix.rstrip('/') + '/'


def record(
    store: Store,
    prefix: str,
    suites: dict[str, Served],
    complete: bool = True,
    note: str = '',
    at: datetime | None = None,
) -> Event:
    "Write one event; returns it with its key"
    at = at or datetime.now(timezone.utc)
    host = socket.gethostname()
    event = Event(at, prefix, dict(suites), complete, note, host, os.getpid())
    stamp = at.strftime('%Y%m%dT%H%M%S.%fZ')
    event.key = f'{history_prefix(prefix)}{stamp}-{host}-{event.pid}.json'
    store.put_bytes(event.key, event.to_json(), '')
    return event


def events(store: Store, prefix: str) -> list[Event]:
    "Every event of a prefix, oldest first"
    keys = sorted(store.list_sizes(history_prefix(prefix)))
    out = []
    for key in keys:
        data = store.get_bytes(key)
        if data is not None:
            out.append(Event.from_json(data, key))
    return sorted(out, key=lambda e: (e.at, e.key))


@dataclass
class Live:
    "A suite as served at some moment, and since when"

    served: Served
    since: datetime
    event: Event = field(repr=False)


def state_at(
    history: list[Event], at: datetime | None = None
) -> dict[str, Live]:
    """What each suite served at a moment (default: the latest)

    Per suite, the latest event at or before the moment that names it.
    since is when that ref first went live without interruption.
    """
    out: dict[str, Live] = {}
    for event in history:
        if at is not None and event.at > at:
            break
        for suite, served in event.suites.items():
            live = out.get(suite)
            if live is None or live.served.ref != served.ref:
                out[suite] = Live(served, event.at, event)
            else:
                out[suite] = Live(served, live.since, event)
    return out


def served_ever(history: list[Event], suite: str, ref: Ref) -> Event | None:
    "The first event in which the prefix served ref for suite, if any"
    for event in history:
        served = event.suites.get(suite)
        if served is not None and served.ref == ref:
            return event
    return None


def parse_time(text: str) -> datetime:
    "An ISO date or time; a date means its end, naive means UTC"
    if text.endswith(('Z', 'z')):
        # fromisoformat only accepts a Z suffix from Python 3.11 on.
        text = text[:-1] + '+00:00'
    try:
        at = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HistoryError(f'not an ISO date or time: {text!r}') from exc
    if len(text) == 10:  # a bare date: everything live that day
        at = at.replace(hour=23, minute=59, second=59, microsecond=999999)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at


def changes(history: list[Event]) -> list[tuple[Event, dict]]:
    """Each event with what it changed: suite -> (old ref or None, new)

    Suites an event does not name are not changes (a partial event).
    """
    out = []
    now: dict[str, Ref] = {}
    for event in history:
        diff = {}
        for suite, served in sorted(event.suites.items()):
            if now.get(suite) != served.ref:
                diff[suite] = (now.get(suite), served.ref)
                now[suite] = served.ref
        out.append((event, diff))
    return out


def verify(
    store: Store, upstreams: dict[str, Upstream], live: dict[str, Live]
) -> dict[str, str]:
    """Check recorded digests against the snapshots; suite -> problem

    The recorded sha256 must be that of the snapshot's own InRelease (or
    Release) in _snap/. An empty result means every suite checks out.
    upstreams: every upstream serving this prefix, see layout.owner.
    """
    problems = {}
    for suite, entry in sorted(live.items()):
        upstream = owner(upstreams, suite)
        if upstream is None:
            problems[suite] = 'no configured upstream owns this suite'
            continue
        root = snapshot_root(upstream.name, suite, entry.served.ref)
        digests = [
            store.sha256(f'{root}dists/{suite}/{sig}')
            for sig in ('InRelease', 'Release')
        ]
        digests = [d for d in digests if d]
        if not digests:
            problems[suite] = f'snapshot {entry.served.ref} is gone'
        elif entry.served.release_sha256 != digests[0]:
            problems[suite] = (
                f'recorded {entry.served.release_sha256}, '
                f'snapshot has {digests[0]}'
            )
    return problems
