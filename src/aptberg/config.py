"""YAML configuration loaded into frozen dataclasses

Relative paths (keyrings, include/exclude files, scratch) are resolved
against the directory holding the config file.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import user_agent
from .filter import FOLLOW


class ConfigError(Exception):
    "The configuration is invalid"


@dataclass(frozen=True)
class Filter:
    """Which package names to mirror

    All three lists hold fnmatch globs; a pattern without glob characters
    is a literal name. Precedence, strongest first:

    - hard_exclude: never mirrored, even when something depends on it;
      such dependencies are broken on purpose and reported
    - literal include names: always seeds
    - soft_exclude: drops names an include glob matched; dependency
      following can still bring them back
    - include globs: seeds ("*" is everything)

    The closure then follows the relation fields in follow.
    """

    include: tuple[str, ...]
    soft_exclude: tuple[str, ...] = ()
    hard_exclude: tuple[str, ...] = ()
    follow: tuple[str, ...] = FOLLOW


@dataclass(frozen=True)
class Source:
    """One fetchable upstream tree

    An upstream without channels has one source, with tree None. An
    upstream with channels has one source per distinct tree.
    """

    upstream: str
    tree: str | None
    url: str

    @property
    def scratch_name(self) -> Path:
        "Where this source lives under the scratch directory"
        return Path(self.upstream, *([self.tree] if self.tree else []))


@dataclass(frozen=True)
class Channel:
    "A channel of a versioned upstream and the tree it follows"

    tree: str
    url: str


@dataclass(frozen=True)
class Upstream:
    """A signed upstream: where it lives and what we take from it

    components maps each suite to the components fetched for it,
    explicitly, suite by suite -- there is no separate list of suite
    names, and no shared components list, to drift out of sync with
    what each suite actually carries.
    """

    name: str
    keyring: Path
    components: dict[str, tuple[str, ...]]
    architectures: tuple[str, ...]
    url: str | None = None
    channels: dict[str, Channel] = field(default_factory=dict)
    filter: Filter | None = None
    group: str | None = None
    # mirror each component's Sources index and the files it names, for
    # deb-src; on by default, a component without one contributes nothing
    deb_src: bool = True
    # None: this upstream's files live in _pool/<unit>/, its own
    # namespace: a Filename: is only a safe dedup key within one
    # archive's own release process (see docs/FINDINGS.rst), so nothing is
    # shared by default. Set to name the pool to use instead, _pool/<name>/
    # -- e.g. _shared, for upstreams that are meant to share one.
    pool: str | None = None
    # None: served at <name>/ch/<stage>. Set to have this upstream
    # served at another upstream's path instead -- an EOL suite moved to
    # a different host (jessie to archive.debian.org) still needs its
    # own name (own keyring, own url, own fetch/cut schedule, own
    # _snap/), but hosts running it must not see a different path than
    # hosts on the upstream's still-current suites. Only the served path
    # changes: _snap/, group and pool key off name and group as ever.
    path: str | None = None
    # A flat repository: Release, Packages and the .debs all sit at url
    # itself, with no dists/ tree and no components (NVIDIA's CUDA
    # repositories). Its single suite is named after the upstream. See
    # layout.py for how it is served.
    flat: bool = False
    # Flat only: the release this repository is built for ("noble"). A
    # flat Release names no codename, so without this the upstream
    # joins every family, like an anydist suite.
    codename: str | None = None
    # Allowed Release fields (origin, label): every suite must match
    # at least one of these mappings, or the fetch is refused. Clients
    # pin on these (apt_preferences o=, l=) and the Release is served
    # untouched, so an upstream that starts claiming another Origin
    # would walk out of such a pin unnoticed. More than one mapping
    # is for an upstream whose suites differ (debian vs. backports).
    origin_pins: list[dict[str, str]] = field(default_factory=list)

    @property
    def suites(self) -> tuple[str, ...]:
        "The configured suite names, in config order"
        return tuple(self.components)

    @property
    def pool_name(self) -> str:
        "The name of the _pool/ namespace this upstream's files live in"
        return self.pool or self.unit

    @property
    def unit(self) -> str:
        "What this upstream is fetched and cut together with"
        return self.group or self.name

    @property
    def served(self) -> str:
        "The top-level served path: <served>/ch/<stage>"
        return self.path or self.name

    def sources(self) -> list[Source]:
        "The trees to fetch; channels sharing a tree share its source"
        if not self.channels:
            return [Source(self.name, None, self.url)]
        trees: dict[str, str] = {}
        for _name, ch in self.channels.items():
            if trees.setdefault(ch.tree, ch.url) != ch.url:
                raise ConfigError(f'{self.name}: tree {ch.tree} has two urls')
        return [
            Source(self.name, tree, url) for tree, url in sorted(trees.items())
        ]


@dataclass(frozen=True)
class Config:
    "The whole configuration"

    scratch: Path
    bucket: str
    upstreams: dict[str, Upstream]
    endpoint: str | None = None
    region: str | None = None
    manifests: Path | None = None
    concurrency: int = 8
    download_rate: int | None = None  # bytes per second, all threads
    contact: str | None = None  # added to the User-Agent

    @property
    def user_agent(self) -> str:
        "The User-Agent for upstreams and the bucket"
        return user_agent(self.contact)

    def groups(self) -> dict[str, list[str]]:
        "group -> its upstreams, in config order"
        out: dict[str, list[str]] = {}
        for up in self.upstreams.values():
            if up.group:
                out.setdefault(up.group, []).append(up.name)
        return out

    def served_upstreams(self, served: str) -> dict[str, 'Upstream']:
        "Every upstream serving at this path, keyed by its own name"
        return {
            name: up
            for name, up in self.upstreams.items()
            if up.served == served
        }

    def expand(self, names: list[str]) -> list[str]:
        """Upstream names, each widened to its whole group

        A name may be an upstream or a group. The result keeps config
        order. Upstreams of a group are fetched and cut together: ubuntu
        and ubuntu-security share a dependency closure, kubernetes and
        etcd are deployed side by side.
        """
        groups = self.groups()
        wanted = set()
        for name in names:
            if name in self.upstreams:
                unit = self.upstreams[name].unit
                wanted.update(groups.get(unit, [name]))
            elif name in groups:
                wanted.update(groups[name])
            else:
                raise ConfigError(f'unknown upstream or group {name!r}')
        return [n for n in self.upstreams if n in wanted]


_TOP = {
    'scratch',
    'bucket',
    'endpoint',
    'region',
    'manifests',
    'concurrency',
    'download_rate',
    'contact',
    'upstreams',
}
_UPSTREAM = {
    'url',
    'keyring',
    'suites',
    'architectures',
    'channels',
    'filter',
    'group',
    'deb_src',
    'enabled',
    'pool',
    'path',
    'flat',
    'codename',
    'origin_pins',
}
_ORIGIN_PINS = {'origin', 'label'}
_FILTER = {'include', 'soft_exclude', 'hard_exclude', 'follow'}
_NAME = re.compile(r'[a-z0-9][a-z0-9.+-]*')
# A pool name may also start with _, which no unit does: _shared cannot
# be somebody's own pool by accident.
_POOL = re.compile(r'_?[a-z0-9][a-z0-9.+-]*')


_RATE = re.compile(r'(\d+(?:\.\d+)?)\s*([kmg]?)b?(?:/s)?', re.I)


def _rate(value: object) -> int | None:
    "Bytes per second from 300M, '1.5G', 5000000 and the like"
    if value is None:
        return None
    m = _RATE.fullmatch(str(value).strip())
    rate = (
        int(float(m[1]) * 1000 ** ('bkmg'.index(m[2].lower() or 'b')))
        if m
        else 0
    )
    if rate <= 0:
        raise ConfigError(f'config: bad download_rate {value!r}')
    return rate


def load(path: Path | str) -> Config:
    "Load and validate a config file"
    path = Path(path)
    with path.open() as fh:
        raw = yaml.safe_load(fh) or {}
    # Absolute, so nothing depends on the working directory later on.
    base = path.resolve().parent
    _check_keys('config', raw, _TOP, required={'scratch', 'bucket'})
    # enabled: no drops the upstream before it is otherwise validated, so
    # a source can be switched off without also keeping the rest of its
    # entry valid, e.g. while it is being edited.
    upstreams = {
        name: _upstream(name, body or {}, base)
        for name, body in (raw.get('upstreams') or {}).items()
        if (body or {}).get('enabled', True)
    }
    if not upstreams:
        raise ConfigError('config: no upstreams')
    for up in upstreams.values():
        # A group may share its name with one of its own members
        # (ubuntu, ubuntu-security in group ubuntu), not with an outsider.
        other = upstreams.get(up.group or '')
        if other is not None and other.group != up.group:
            raise ConfigError(
                f'upstreams.{up.name}: group {up.group!r} is also the '
                f'name of an upstream outside it'
            )
    _check_served_suites(upstreams)
    _check_flat_alone(upstreams)
    manifests = raw.get('manifests')
    return Config(
        scratch=base / raw['scratch'],
        bucket=raw['bucket'],
        upstreams=upstreams,
        endpoint=raw.get('endpoint'),
        region=raw.get('region'),
        manifests=base / manifests if manifests else None,
        concurrency=int(raw.get('concurrency', 8)),
        download_rate=_rate(raw.get('download_rate')),
        contact=str(raw['contact']) if raw.get('contact') else None,
    )


def _check_served_suites(upstreams: dict[str, Upstream]) -> None:
    """Two upstreams sharing a served path must not both name a suite

    Both would resolve to the same dists/<suite>/ at that path, and
    every reader that has to work out which upstream owns a suite it
    finds there (layout.owner) relies on this being unambiguous.
    """
    by_served: dict[str, dict[str, str]] = {}
    for up in upstreams.values():
        seen = by_served.setdefault(up.served, {})
        for suite in up.suites:
            clash = seen.get(suite)
            if clash is not None:
                raise ConfigError(
                    f'{up.served}: suite {suite!r} is in both '
                    f'upstreams.{clash} and upstreams.{up.name}'
                )
            seen[suite] = up.name


def _check_flat_alone(upstreams: dict[str, Upstream]) -> None:
    """A flat upstream must not share its served path with another

    It serves Release and Packages at the top of its channel prefixes,
    where a second upstream's dists/ would sit beside them and every
    reader that lists a prefix could not tell whose files it found.
    """
    for up in upstreams.values():
        if up.flat:
            others = [
                n
                for n, other in upstreams.items()
                if n != up.name and other.served == up.served
            ]
            if others:
                raise ConfigError(
                    f'upstreams.{up.name}: flat, but {up.served!r} is '
                    f'also served by {", ".join(others)}'
                )


def read_patterns(path: Path) -> tuple[str, ...]:
    """Read a pattern file: one package name or fnmatch glob per line

    Blank lines and #-comments are skipped.
    """
    with path.open() as fh:
        return _patterns(fh, str(path))


def _patterns(lines, where: str) -> tuple[str, ...]:
    out = []
    for lineno, line in enumerate(lines, 1):
        pattern = str(line).split('#', 1)[0].strip()
        if not pattern:
            continue
        if any(c in pattern for c in ' :,|()'):
            raise ConfigError(
                f'{where}:{lineno}: not a package name or glob: {pattern!r}'
            )
        out.append(pattern)
    return tuple(dict.fromkeys(out))


def _pattern_list(value, base: Path, where: str) -> tuple[str, ...]:
    "A file path (string) or an inline list of patterns"
    if value is None:
        return ()
    if isinstance(value, str):
        return read_patterns(base / value)
    if isinstance(value, list):
        return _patterns(value, where)
    raise ConfigError(f'{where}: expected a file path or a list')


def _components(where: str, body: dict) -> dict[str, tuple[str, ...]]:
    """suite -> its components, from suites: (a mapping)

    Every suite names its own components explicitly, even when several
    suites happen to share the same list.
    """
    suites = body['suites']
    if not isinstance(suites, dict):
        raise ConfigError(
            f'{where}.suites: expected a mapping of suite to its components'
        )
    if not suites:
        raise ConfigError(f'{where}.suites: empty')
    out = {}
    for suite, comps in suites.items():
        if not comps:
            raise ConfigError(f'{where}.suites.{suite}: empty')
        out[str(suite)] = tuple(comps)
    return out


def _upstream(name: str, body: dict, base: Path) -> Upstream:
    where = f'upstreams.{name}'
    # The name is the top-level key prefix of the upstream's served tree;
    # names starting with _ belong to aptberg (_pool, _snap, _lock).
    if not _NAME.fullmatch(str(name)):
        raise ConfigError(
            f'{where}: an upstream name is lowercase '
            f'letters, digits and .+-, not starting with _'
        )
    flat = bool(body.get('flat', False))
    _check_keys(
        where,
        body,
        _UPSTREAM,
        required={'keyring'}
        | (set() if flat else {'suites', 'architectures'}),
    )
    if ('url' in body) == ('channels' in body):
        raise ConfigError(f'{where}: needs exactly one of url, channels')
    override = body.get('pool')
    if override is not None and not _POOL.fullmatch(str(override)):
        raise ConfigError(
            f'{where}: pool is a name of lowercase '
            f'letters, digits and .+-, like _shared'
        )
    origin_pins = _origin_pins(where, body.get('origin_pins') or [])
    return Upstream(
        name=name,
        keyring=base / body['keyring'],
        components=_suites(where, name, body, flat),
        architectures=tuple(body.get('architectures', ())),
        url=_url(body['url']) if 'url' in body else None,
        channels=_channels(where, body.get('channels') or {}),
        filter=_filter(f'{where}.filter', body.get('filter'), base),
        group=_optional_name(f'{where}.group', body.get('group')),
        deb_src=bool(body.get('deb_src', True)),
        pool=str(override) if override is not None else None,
        path=_optional_name(f'{where}.path', body.get('path')),
        flat=flat,
        codename=str(body['codename']) if 'codename' in body else None,
        origin_pins=origin_pins,
    )


def _suites(
    where: str, name: str, body: dict, flat: bool
) -> dict[str, tuple[str, ...]]:
    "suite -> its components; a flat repository's one suite has none"
    if flat:
        banned = {'suites', 'architectures', 'channels'} & set(body)
        if banned:
            raise ConfigError(
                f'{where}: a flat repository has no {sorted(banned)}'
            )
        # One suite, named after the upstream; no components to take.
        return {name: ()}
    if 'codename' in body:
        raise ConfigError(
            f'{where}: codename is for flat '
            f'repositories; a Release names its own'
        )
    return _components(where, body)


def _channels(where: str, raw: dict) -> dict[str, Channel]:
    "channels: name -> its tree and url"
    out = {}
    for name, channel in raw.items():
        _check_keys(
            f'{where}.channels.{name}',
            channel,
            {'tree', 'url'},
            required={'tree', 'url'},
        )
        out[name] = Channel(str(channel['tree']), _url(channel['url']))
    return out


def _filter(where: str, raw: dict | None, base: Path) -> Filter | None:
    "filter:, its pattern lists read (from files, where named)"
    if raw is None:
        return None
    _check_keys(where, raw, _FILTER, required={'include'})
    lists = {
        key: _pattern_list(raw.get(key), base, f'{where}.{key}')
        for key in ('include', 'soft_exclude', 'hard_exclude')
    }
    if not lists['include']:
        raise ConfigError(f'{where}.include: empty; use ["*"] for everything')
    return Filter(**lists, follow=tuple(raw.get('follow', FOLLOW)))


def _optional_name(where: str, value) -> str | None:
    "group: or path:, which name a top-level prefix like upstreams do"
    if value is None:
        return None
    if not _NAME.fullmatch(str(value)):
        raise ConfigError(
            f'{where}: lowercase letters, digits and .+-, not starting with _'
        )
    return str(value)


def _origin_pins(where: str, raw) -> list[dict[str, str]]:
    "origin_pins: the allowed Origin/Label pairs"
    if not isinstance(raw, list):
        raise ConfigError(f'{where}.origin_pins: expected a list of mappings')
    for i, allowed in enumerate(raw):
        if not isinstance(allowed, dict) or not allowed:
            raise ConfigError(
                f'{where}.origin_pins[{i}]: expected a mapping '
                f'of origin and/or label'
            )
        _check_keys(f'{where}.origin_pins[{i}]', allowed, _ORIGIN_PINS)
    return [{k: str(v) for k, v in allowed.items()} for allowed in raw]


def _url(url: str) -> str:
    return str(url).rstrip('/')


def _check_keys(
    where: str, body: dict, allowed: set[str], required: set[str] = frozenset()
) -> None:
    if not isinstance(body, dict):
        raise ConfigError(f'{where}: expected a mapping')
    unknown = set(body) - allowed
    if unknown:
        raise ConfigError(f'{where}: unknown keys {sorted(unknown)}')
    lacking = required - set(body)
    if lacking:
        raise ConfigError(f'{where}: missing keys {sorted(lacking)}')
