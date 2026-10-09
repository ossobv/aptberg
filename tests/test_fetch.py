import hashlib

import httpx
import pytest

from aptberg import fetch
from aptberg.config import Upstream
from aptberg.fetch import FetchError, fetch_suite, load_suite
from aptberg.release import SignatureError

from .helpers import Archive, other_signer, signer, stanza


def _upstream(**kw) -> Upstream:
    args = dict(
        name='ubuntu',
        keyring=signer().keyring,
        components={'noble': ('main',)},
        architectures=('amd64', 'arm64'),
        url='http://up.example/ubuntu',
    )
    args.update(kw)
    return Upstream(**args)


def _archive(**kw) -> Archive:
    archive = Archive()
    archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [stanza('a')],
            'main/binary-arm64': [stanza('a', arch='arm64')],
        },
        **kw,
    )
    return archive


def _fetch(archive, tmp_path, up=None, suite='noble'):
    up = up or _upstream()
    (source,) = up.sources()
    return fetch_suite(archive.client(), up, source, suite, tmp_path)


def test_fetch_suite(tmp_path):
    "By-hash first, compressed only, signatures kept"
    archive = _archive()
    fs = _fetch(archive, tmp_path)
    got = sorted(
        p.relative_to(fs.path).as_posix()
        for p in fs.path.rglob('*')
        if p.is_file()
    )
    assert got == [
        '.fetch-run',
        'Contents-amd64.gz',
        'InRelease',
        'Release',
        'Release.gpg',
        'main/binary-amd64/Packages.gz',
        'main/binary-amd64/Packages.xz',
        'main/binary-arm64/Packages.gz',
        'main/binary-arm64/Packages.xz',
        'main/i18n/Translation-en.xz',
    ]
    assert fs.path == tmp_path / 'ubuntu' / 'noble'
    fetched = [r for r in archive.requests if '/binary-' in r]
    assert fetched and all('/by-hash/SHA256/' in r for r in fetched)
    assert fs.family == 'noble'


def test_fetch_suite_binary_all_not_served(tmp_path):
    "Old archives list binary-all in the Release but never served it"
    archive = Archive()
    archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [stanza('a')],
            'main/binary-all': [stanza('b', arch='all')],
        },
    )
    archive.files = {
        path: data
        for path, data in archive.files.items()
        if '/binary-all/' not in path
        and not ('/by-hash/' in path and data.startswith(b'Package: b'))
    }
    up = _upstream(architectures=('amd64',))
    fs = _fetch(archive, tmp_path, up=up)
    assert not any('binary-all' in e.path for e in fs.entries)
    assert any('binary-amd64' in e.path for e in fs.entries)
    assert [
        e.path
        for e in load_suite(up, up.sources()[0], 'noble', tmp_path).entries
    ] == [e.path for e in fs.entries]


def test_fetch_suite_missing_index_is_an_error(tmp_path):
    "Only binary-all may be listed without being served"
    archive = _archive()
    archive.files = {
        path: data
        for path, data in archive.files.items()
        if 'binary-amd64' not in path
        and not ('/by-hash/' in path and data.startswith(b'Package: a'))
    }
    with pytest.raises(FetchError, match='not found upstream'):
        _fetch(archive, tmp_path)


def test_fetch_suite_contents(tmp_path):
    "Contents-<arch> is always pulled in"
    archive = _archive()
    fs = _fetch(archive, tmp_path)
    got = sorted(
        p.relative_to(fs.path).as_posix()
        for p in fs.path.rglob('*')
        if p.is_file()
    )
    assert 'Contents-amd64.gz' in got


def test_fetch_run_stamped(tmp_path):
    "Each suite records the run that fetched it; load_suite reads it back"
    archive = _archive()
    up = _upstream()
    (source,) = up.sources()
    fs = fetch_suite(archive.client(), up, source, 'noble', tmp_path, 'r1')
    assert fs.run == 'r1'
    assert load_suite(up, source, 'noble', tmp_path).run == 'r1'
    fetch_suite(archive.client(), up, source, 'noble', tmp_path, 'r2')
    assert load_suite(up, source, 'noble', tmp_path).run == 'r2'


def test_origin_pins_matches(tmp_path):
    "The configured Origin and Label are what the Release carries"
    archive = _archive(headers={'Origin': 'Zabbix', 'Label': 'Zabbix SIA'})
    up = _upstream(origin_pins=[{'origin': 'Zabbix', 'label': 'Zabbix SIA'}])
    fs = _fetch(archive, tmp_path, up=up)
    assert (fs.release.origin, fs.release.label) == ('Zabbix', 'Zabbix SIA')
    load_suite(up, up.sources()[0], 'noble', tmp_path)


def test_origin_pins_any_of_the_list(tmp_path):
    "A suite matching any one mapping passes; pairs are not mixed"
    up = _upstream(
        origin_pins=[
            {'origin': 'Debian', 'label': 'Debian'},
            {'origin': 'Debian Backports', 'label': 'Debian Backports'},
        ]
    )
    archive = _archive(
        headers={'Origin': 'Debian Backports', 'Label': 'Debian Backports'}
    )
    _fetch(archive, tmp_path, up=up)
    archive = _archive(
        headers={'Origin': 'Debian Backports', 'Label': 'Debian'}
    )
    with pytest.raises(
        FetchError,
        match=r"expected Origin 'Debian', "
        r"Label 'Debian' or Origin",
    ):
        _fetch(archive, tmp_path / '2', up=up)


def test_origin_pins_refuses_another_origin(tmp_path):
    "An upstream that starts claiming Origin: Ubuntu is not fetched"
    archive = _archive(headers={'Origin': 'Ubuntu'})
    with pytest.raises(
        FetchError,
        match=r"Origin 'Ubuntu', .*expected "
        r"Origin 'Zabbix'",
    ):
        _fetch(
            archive, tmp_path, up=_upstream(origin_pins=[{'origin': 'Zabbix'}])
        )
    assert not (tmp_path / 'ubuntu' / 'noble' / 'Release').exists()


def test_origin_pins_refuses_a_missing_label(tmp_path):
    "A Release without the field fails too, not just a different one"
    archive = _archive(headers={'Origin': 'Zabbix'})
    with pytest.raises(
        FetchError,
        match=r"Label '', expected Origin "
        r"'Zabbix', Label 'Zabbix'",
    ):
        _fetch(
            archive,
            tmp_path,
            up=_upstream(
                origin_pins=[{'origin': 'Zabbix', 'label': 'Zabbix'}]
            ),
        )


def test_origin_pins_checks_scratch_on_reload(tmp_path):
    "cut from scratch re-checks, in case the config changed since fetch"
    archive = _archive(headers={'Origin': 'Zabbix'})
    _fetch(archive, tmp_path)
    up = _upstream(origin_pins=[{'origin': 'Other'}])
    with pytest.raises(FetchError, match="expected Origin 'Other'"):
        load_suite(up, up.sources()[0], 'noble', tmp_path)


def test_refetch_is_incremental(tmp_path):
    "A second fetch only retrieves the signature files"
    archive = _archive()
    _fetch(archive, tmp_path)
    archive.requests.clear()
    _fetch(archive, tmp_path)
    assert sorted(archive.requests) == [
        '/ubuntu/dists/noble/InRelease',
        '/ubuntu/dists/noble/Release',
        '/ubuntu/dists/noble/Release.gpg',
    ]


def test_no_by_hash_falls_back(tmp_path):
    "Without Acquire-By-Hash the canonical paths are used"
    archive = _archive(by_hash=False)
    _fetch(archive, tmp_path)
    assert not any('by-hash' in r for r in archive.requests)


def test_release_only(tmp_path):
    "An upstream without InRelease works through Release.gpg"
    fs = _fetch(_archive(inrelease=False), tmp_path)
    assert not (fs.path / 'InRelease').exists()
    assert (fs.path / 'Release.gpg').exists()


def test_bad_signature(tmp_path):
    "An untrusted signature stops the fetch before any index is taken"
    archive = _archive(signer_=other_signer())
    with pytest.raises(SignatureError):
        _fetch(archive, tmp_path)
    assert not any('/binary-' in r for r in archive.requests)


def test_corrupt_index(tmp_path):
    "An index not matching the signed Release is refused"
    archive = _archive()
    for path in archive.files:
        if '/binary-amd64/' in path:
            archive.files[path] = b'garbage'
    with pytest.raises(FetchError, match='size|sha256'):
        _fetch(archive, tmp_path)


def test_suite_mismatch(tmp_path):
    "A validly signed Release for another suite is refused"
    archive = Archive()
    archive.add_suite('/ubuntu', 'noble', {}, codename='jammy')
    archive.files = {
        k.replace('noble', 'noble-updates'): v
        for k, v in archive.files.items()
    }
    with pytest.raises(FetchError, match='refusing'):
        _fetch(archive, tmp_path, suite='noble-updates')


def test_prunes_stale_and_reloads(tmp_path):
    "Files the Release no longer names are removed; load_suite agrees"
    archive = _archive()
    fs = _fetch(archive, tmp_path)
    stale = fs.path / 'main/binary-amd64/Packages.bz2'
    stale.write_bytes(b'old')
    _fetch(archive, tmp_path)
    assert not stale.exists()
    (source,) = _upstream().sources()
    again = load_suite(_upstream(), source, 'noble', tmp_path)
    assert again.entries == fs.entries
    (fs.path / 'main/binary-amd64/Packages.xz').write_bytes(b'x')
    with pytest.raises(FetchError):
        load_suite(_upstream(), source, 'noble', tmp_path)


def test_rate_limiter_sleeps_once_burst_is_spent():
    "A second's worth of burst, then sleeps that keep to the rate"
    now = [0.0]
    slept = []

    def sleep(secs):
        slept.append(secs)
        now[0] += secs

    lim = fetch.RateLimiter(100, clock=lambda: now[0], sleep=sleep)
    lim.acquire(100)  # the first second is free
    assert slept == []
    lim.acquire(50)
    assert slept == [0.5]
    now[0] += 10  # idle: the bucket refills, capped
    lim.acquire(100)
    assert slept == [0.5]


def test_rate_limiter_rejects_nonpositive_rate():
    "A rate of 0 would never send anything"
    with pytest.raises(ValueError):
        fetch.RateLimiter(0)


def test_client_carries_limiter_only_with_rate():
    "No download_rate, no limiter"
    assert fetch.client().limiter is None
    assert fetch.client(rate=300e6).limiter.rate == 300e6


def _flaky(*statuses):
    "A client answering each request with the next status; then b'data'"
    left = list(statuses)

    def answer(request):
        status = left.pop(0) if left else 200
        return httpx.Response(
            status, content=b'data' if status == 200 else b''
        )

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_download_retries_server_errors(tmp_path, monkeypatch):
    "A 5xx is retried after a backoff; the file arrives verified"
    slept = []
    monkeypatch.setattr(fetch.time, 'sleep', slept.append)
    dest = tmp_path / 'x.deb'
    sha = hashlib.sha256(b'data').hexdigest()
    assert fetch.download(_flaky(503, 502), 'http://u/x', dest, 4, sha)
    assert dest.read_bytes() == b'data'
    assert slept == [2, 4]


def test_download_gives_up(tmp_path, monkeypatch):
    "A 4xx fails at once, a 5xx after ATTEMPTS; no partial file is left"
    monkeypatch.setattr(fetch.time, 'sleep', lambda s: None)
    dest = tmp_path / 'x.deb'
    with pytest.raises(FetchError, match='403'):
        fetch.download(_flaky(403), 'http://u/x', dest)
    with pytest.raises(FetchError, match='500'):
        fetch.download(_flaky(*[500] * fetch.ATTEMPTS), 'http://u/x', dest)
    assert list(tmp_path.iterdir()) == []
    assert fetch.download(_flaky(404), 'http://u/x', dest) is False


def test_download_refuses_other_bytes(tmp_path):
    "A size or sha256 other than the index's is an error"
    dest = tmp_path / 'x.deb'
    with pytest.raises(FetchError, match='size 4, expected 5'):
        fetch.download(_flaky(), 'http://u/x', dest, size=5)
    with pytest.raises(FetchError, match='sha256 mismatch'):
        fetch.download(_flaky(), 'http://u/x', dest, sha256='0' * 64)
    assert list(tmp_path.iterdir()) == []


def test_download_retry_rewinds_progress(tmp_path, monkeypatch):
    "Bytes counted before a dropped connection are taken back"
    monkeypatch.setattr(fetch.time, 'sleep', lambda s: None)
    calls = []

    class Drops(httpx.SyncByteStream):
        def __iter__(self):
            yield bytes(fetch.CHUNK)
            raise httpx.ReadError('dropped')

    def answer(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, stream=Drops())
        return httpx.Response(200, content=b'data')

    http = httpx.Client(transport=httpx.MockTransport(answer))
    counted = []
    fetch.download(http, 'http://u/x', tmp_path / 'x', progress=counted.append)
    assert counted == [fetch.CHUNK, -fetch.CHUNK, 4]
