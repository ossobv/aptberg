"""HTTP retrieval of upstream suites into scratch, verified

Scratch layout, per suite:

    <scratch>/<upstream>/[<tree>/]<suite>/InRelease
    <scratch>/<upstream>/[<tree>/]<suite>/Release, Release.gpg
    <scratch>/<upstream>/[<tree>/]<suite>/main/binary-amd64/Packages.xz
    ...

Paths below the suite directory are relative to dists/<suite>/, exactly
as the Release names them. Index files are written only after their
sha256 and size match the verified Release; the signature files are
written last, so an interrupted fetch leaves the old ones in place.
"""

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from . import index, user_agent
from .config import Source, Upstream
from .release import Entry, Release, family, verified_payload

log = logging.getLogger(__name__)

CHUNK = 1 << 20
ATTEMPTS = 4


class FetchError(Exception):
    "An upstream file could not be retrieved or did not verify"


class RateLimiter:
    """Cap the aggregate throughput of all threads sharing it

    A token bucket holding one second of budget: short bursts pass at
    line speed, the long-run average stays at rate bytes per second.
    """

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError('rate must be positive')
        self.rate = float(rate)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._tokens = self.rate
        self._stamp = clock()

    def acquire(self, nbytes: int) -> None:
        "Block until nbytes may be used"
        with self._lock:
            now = self._clock()
            self._tokens = min(
                self.rate, self._tokens + (now - self._stamp) * self.rate
            )
            self._stamp = now
            # Go into debt; whoever comes next waits behind us.
            self._tokens -= nbytes
            wait = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if wait:
            self._sleep(wait)


class Client(httpx.Client):
    "An httpx.Client that carries the download rate limit, if any"

    limiter: RateLimiter | None = None


def client(
    concurrency: int = 8, agent: str | None = None, rate: float | None = None
) -> Client:
    """A pooled HTTP client sized for concurrency parallel downloads

    rate caps the combined download speed in bytes per second.
    """
    http = Client(
        follow_redirects=True,
        timeout=httpx.Timeout(60.0, connect=15.0),
        limits=httpx.Limits(
            max_connections=concurrency * 2,
            max_keepalive_connections=concurrency * 2,
        ),
        headers={'User-Agent': agent or user_agent()},
    )
    if rate:
        http.limiter = RateLimiter(rate)
    return http


def download(
    http: httpx.Client,
    url: str,
    dest: Path,
    size: int | None = None,
    sha256: str | None = None,
    progress: Callable[[int], None] | None = None,
) -> bool:
    """Stream url to dest, verifying size and sha256 when given

    Returns False on 404. Transport errors and 5xx are retried. dest only
    appears once the content has verified.
    """
    tmp = dest.with_name(dest.name + '.part')
    limiter = getattr(http, 'limiter', None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, ATTEMPTS + 1):
        done = 0
        try:
            with http.stream('GET', url) as resp:
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                digest = hashlib.sha256()
                with tmp.open('wb') as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        _write_chunk(fh, chunk, digest, limiter, progress)
                        done += len(chunk)
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if progress and done:
                progress(-done)
            status = getattr(
                getattr(exc, 'response', None), 'status_code', 500
            )
            if attempt == ATTEMPTS or status < 500:
                tmp.unlink(missing_ok=True)
                raise FetchError(f'{url}: {exc}') from exc
            log.warning('%s: %s, retrying', url, exc)
            time.sleep(2**attempt)
    if size is not None and done != size:
        tmp.unlink(missing_ok=True)
        raise FetchError(f'{url}: size {done}, expected {size}')
    if sha256 is not None and digest.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise FetchError(f'{url}: sha256 mismatch')
    os.replace(tmp, dest)
    return True


def _write_chunk(fh, chunk: bytes, digest, limiter, progress) -> None:
    "One downloaded chunk: rate limited, written, hashed and counted"
    if limiter:
        limiter.acquire(len(chunk))
    fh.write(chunk)
    digest.update(chunk)
    if progress:
        progress(len(chunk))


def file_sha256(path: Path) -> str:
    "The sha256 of a local file"
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def matches(path: Path, entry: Entry) -> bool:
    "Whether path already holds exactly the entry's content"
    try:
        if path.stat().st_size != entry.size:
            return False
    except FileNotFoundError:
        return False
    return file_sha256(path) == entry.sha256


@dataclass(frozen=True)
class FetchedSuite:
    "A suite verified into scratch"

    upstream: Upstream
    source: Source
    suite: str
    release: Release
    path: Path
    entries: tuple[Entry, ...]
    # sha256 of the verified Release text: equal means upstream has not
    # moved
    release_sha256: str
    # the fetch run that wrote this suite into scratch ("" if unknown)
    run: str = ''

    @property
    def codename(self) -> str:
        "The release this suite is for: its Release's, else the config's"
        return self.release.codename or self.upstream.codename or ''

    @property
    def family(self) -> str:
        "The release grouping this suite with its siblings"
        return family(self.codename or self.suite)


SIGNATURES = ('InRelease', 'Release', 'Release.gpg')
# Written last into each suite directory: the run that fetched it. cut
# refuses to mix runs within a group.
RUN = '.fetch-run'


def suite_url(upstream: Upstream, source: Source, suite: str) -> str:
    "Where upstream serves the Release and indexes of a suite"
    return source.url if upstream.flat else f'{source.url}/dists/{suite}'


def check_origin_pins(
    upstream: Upstream, release: Release, where: str
) -> None:
    """Refuse a Release whose Origin and Label are not an allowed pair

    Clients pin on these fields, and the Release is served as signed, so
    a change would silently take its packages in or out of a pin.
    """
    found = {'origin': release.origin, 'label': release.label}
    if not upstream.origin_pins or any(
        all(found[key] == want for key, want in allowed.items())
        for allowed in upstream.origin_pins
    ):
        return
    options = ' or '.join(
        ', '.join(
            f'{key.capitalize()} {want!r}' for key, want in allowed.items()
        )
        for allowed in upstream.origin_pins
    )
    raise FetchError(
        f'{where}: Release has Origin {found["origin"]!r}, Label '
        f'{found["label"]!r}, expected {options}. Clients may pin '
        f'on these (apt_preferences o=, l=); if upstream really '
        f'changed them, review that and update origin_pins: in the config'
    )


def fetch_suite(
    http: httpx.Client,
    upstream: Upstream,
    source: Source,
    suite: str,
    scratch: Path,
    run: str = '',
) -> FetchedSuite:
    "Retrieve, verify and select the indexes of one suite into scratch"
    dest = scratch / source.scratch_name / suite
    incoming = dest / '.incoming'
    incoming.mkdir(parents=True, exist_ok=True)
    base = suite_url(upstream, source, suite)
    payload = _signed_release(http, base, incoming, upstream.keyring)
    release = Release.parse(payload)
    _check_release(upstream, release, base, suite)
    entries = _fetch_entries(
        http, base, release, index.wanted(release, upstream, suite), dest
    )
    _settle(dest, {entry.path for entry in entries}, run)
    log.info('%s: %d index files, %s', base, len(entries), release.date)
    return FetchedSuite(
        upstream,
        source,
        suite,
        release,
        dest,
        tuple(entries),
        _sha256(payload),
        run,
    )


def _check_release(
    upstream: Upstream, release: Release, base: str, suite: str
) -> None:
    "Refuse a Release that is not the suite's or not its origin's"
    # A flat Release names no suite, codename, components or
    # architectures: the URL and the signature are all it vouches for.
    if not upstream.flat and suite not in (release.suite, release.codename):
        raise FetchError(
            f'{base}: Release is for suite {release.suite!r}, codename '
            f'{release.codename!r}; refusing it for {suite!r}'
        )
    check_origin_pins(upstream, release, base)
    # Components are listed per suite, so a missing one is a config
    # mistake. Architectures are one list for all suites of an upstream
    # (focal has no amd64v3): a missing one is just a fact about this
    # suite, and apt does not ask for what the Release does not list.
    for name in upstream.components[suite]:
        if name not in release.components:
            log.warning('%s: component %s not in Release', base, name)
    absent = [
        a for a in upstream.architectures if a not in release.architectures
    ]
    if absent:
        log.info(
            '%s: no such architecture in Release: %s', base, ', '.join(absent)
        )


def _fetch_entries(
    http: httpx.Client,
    base: str,
    release: Release,
    entries: list[Entry],
    dest: Path,
) -> list[Entry]:
    "Fetch the entries scratch lacks; returns those upstream serves"
    served = []
    for entry in entries:
        target = dest / entry.path
        if not matches(target, entry) and not _fetch_entry(
            http, base, release, entry, target
        ):
            log.warning(
                '%s/%s: listed in Release but not served; skipping',
                base,
                entry.path,
            )
            continue
        served.append(entry)
    return served


def _settle(dest: Path, keep: set[str], run: str) -> None:
    """Make a suite's scratch directory hold this fetch and nothing else

    Index files the Release no longer names go; the new signature files
    move in from .incoming/ (only now, with every index in place), and
    the run stamp says which fetch this was.
    """
    incoming = dest / '.incoming'
    for path in dest.rglob('*'):
        rel = path.relative_to(dest).as_posix()
        if (
            path.is_file()
            and rel not in keep
            and rel not in SIGNATURES
            and rel != RUN
            and not rel.startswith('.incoming/')
        ):
            path.unlink()
    for name in SIGNATURES:
        new = incoming / name
        if new.exists():
            os.replace(new, dest / name)
        else:
            (dest / name).unlink(missing_ok=True)
    incoming.rmdir()
    (dest / RUN).write_text(run + '\n')


def probe_family(
    http: httpx.Client | None,
    upstream: Upstream,
    source: Source,
    suite: str,
    scratch: Path,
) -> str:
    """The family (codename) of a suite, from its verified Release alone

    Without http the Release in scratch is read, else a fresh one is
    downloaded and thrown away: either way no index is touched.
    """
    if http is None:
        dest = scratch / source.scratch_name / suite
        codename = Release.parse(
            _scratch_payload(dest, upstream.keyring)
        ).codename
        return family(codename or upstream.codename or suite)
    scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='probe-', dir=scratch) as tmp:
        payload = _signed_release(
            http,
            suite_url(upstream, source, suite),
            Path(tmp),
            upstream.keyring,
        )
    return family(
        Release.parse(payload).codename or upstream.codename or suite
    )


def _scratch_payload(dest: Path, keyring: Path) -> str:
    "The verified Release text a suite in scratch carries"
    if (dest / 'InRelease').exists():
        return verified_payload(dest / 'InRelease', keyring)
    return verified_payload(dest / 'Release', keyring, dest / 'Release.gpg')


def load_suite(
    upstream: Upstream, source: Source, suite: str, scratch: Path
) -> FetchedSuite:
    """Reload a suite from scratch without touching the network

    The signature is verified again and every selected entry must match.
    """
    dest = scratch / source.scratch_name / suite
    payload = _scratch_payload(dest, upstream.keyring)
    release = Release.parse(payload)
    check_origin_pins(upstream, release, str(dest))
    entries = index.wanted(release, upstream, suite)
    served = []
    for entry in entries:
        if not matches(dest / entry.path, entry):
            if index.optional(entry.path) and not (dest / entry.path).exists():
                continue  # upstream never served it (see fetch_suite)
            raise FetchError(f'{dest / entry.path}: not as Release says')
        served.append(entry)
    entries = served
    try:
        run = (dest / RUN).read_text().strip()
    except FileNotFoundError:
        run = ''
    return FetchedSuite(
        upstream,
        source,
        suite,
        release,
        dest,
        tuple(entries),
        _sha256(payload),
        run,
    )


def _signed_release(
    http: httpx.Client, base: str, incoming: Path, keyring: Path
) -> str:
    """Retrieve InRelease and/or Release + Release.gpg, verified

    Whatever of the two forms upstream serves is kept, so clients using
    either find it. When both exist they must carry the same payload.
    """
    for name in SIGNATURES:
        (incoming / name).unlink(missing_ok=True)
    payloads = []
    if download(http, f'{base}/InRelease', incoming / 'InRelease'):
        payloads.append(verified_payload(incoming / 'InRelease', keyring))
    if download(http, f'{base}/Release', incoming / 'Release'):
        if download(http, f'{base}/Release.gpg', incoming / 'Release.gpg'):
            payloads.append(
                verified_payload(
                    incoming / 'Release', keyring, incoming / 'Release.gpg'
                )
            )
        else:
            # An unsigned Release next to a good InRelease is useless to
            # clients and we will not serve it.
            (incoming / 'Release').unlink()
    if not payloads:
        raise FetchError(f'{base}: no signed InRelease or Release')
    if len(payloads) == 2 and payloads[0] != payloads[1]:
        raise FetchError(
            f'{base}: InRelease and Release differ; upstream moved '
            f'mid-fetch? Rerun.'
        )
    return payloads[0]


def _fetch_entry(
    http: httpx.Client, base: str, release: Release, entry: Entry, target: Path
) -> bool:
    """Download entry into target

    Returns False when upstream does not serve an index it is allowed to
    list without serving (index.optional); any other absence is an error.
    """
    urls = []
    if release.acquire_by_hash:
        urls.append(f'{base}/{index.by_hash_dir(entry.path)}/{entry.sha256}')
    urls.append(f'{base}/{entry.path}')
    for url in urls:
        if download(http, url, target, entry.size, entry.sha256):
            return True
    if index.optional(entry.path):
        return False
    raise FetchError(f'{base}/{entry.path}: not found upstream')


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
