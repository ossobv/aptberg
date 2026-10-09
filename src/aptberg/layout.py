"""Where a channel prefix keeps the suites it serves

An ordinary upstream is served at <prefix>dists/<suite>/: Release, and
the indexes below it. A flat upstream is served at <prefix> itself:
Release, Packages.gz and the rest sit at the top, because its signed
Release names its indexes relative to the repository root and its
Packages name the .debs as ./x.deb, so nothing can be moved without
invalidating NVIDIA's signature (we have no key of our own to re-sign
with). Clients use "deb <prefix> ./" and the front end rewrites
<prefix>x.deb to the matching _pool/ object, as it does for pool/.

Snapshots in _snap/ do not care: they hold dists/<suite>/ for both, and
only apply decides where a copy of it lands.
"""

from collections.abc import Iterable

from .config import Upstream
from .fetch import SIGNATURES
from .store import Store


def dists(upstream: Upstream, prefix: str, suite: str) -> str:
    "The key prefix of a suite's Release and indexes below a channel prefix"
    return prefix if upstream.flat else f'{prefix}dists/{suite}/'


def owner(upstreams: dict[str, Upstream], suite: str) -> Upstream | None:
    """The upstream a suite under a shared channel prefix belongs to

    upstreams: every upstream serving at the prefix (Config.
    served_upstreams). The one whose suites: lists it, as config.load
    allows only one; else, when the prefix has only one upstream, that
    one: a suite dropped from suites: is still its own. None when no
    upstream lists it and several could have: which _snap/ tree it is
    under is not ours to guess.
    """
    for upstream in upstreams.values():
        if suite in upstream.suites:
            return upstream
    if len(upstreams) == 1:
        return next(iter(upstreams.values()))
    return None


def listing_root(upstreams: Iterable[Upstream], prefix: str) -> str:
    """The key prefix whose listing holds everything a prefix serves

    upstreams: every upstream serving at the prefix. A flat one cannot
    share a served path (config.load), so one flat member means all are.
    """
    if any(up.flat for up in upstreams):
        return prefix
    return f'{prefix}dists/'


def served_suites(
    store: Store, upstreams: Iterable[Upstream], prefix: str
) -> list[str]:
    "The suites that have files under a channel prefix, sorted"
    found = set(store.list_dirs(f'{prefix}dists/'))
    for up in upstreams:
        if up.flat and any(store.sha256(prefix + name) for name in SIGNATURES):
            found.update(up.suites)
    return sorted(found)
