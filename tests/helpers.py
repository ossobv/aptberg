"""Test helpers: a throwaway signing key and a fake apt archive"""

import atexit
import gzip
import hashlib
import lzma
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import date, datetime, timezone
from functools import cache
from pathlib import Path

import httpx

from aptberg import pool
from aptberg.apply import run
from aptberg.config import Channel, Filter, Upstream
from aptberg.fetch import fetch_suite
from aptberg.manifest import Manifest
from aptberg.plan import build
from aptberg.pool import PREFIX, select
from aptberg.snapshot import cut, record


def sync(selection, store, *args, **kwargs):
    "pool.sync, listing the whole of the test bucket's _pool/ unless told"
    kwargs.setdefault('present', store.list_etags(PREFIX))
    return pool.sync(selection, store, *args, **kwargs)


class Signer:
    "A fresh GnuPG key in a temporary home, exported as a keyring"

    def __init__(self, uid: str = 'aptberg test <test@example.com>'):
        self.home = Path(tempfile.mkdtemp(prefix='aptberg-test-gpg-'))
        atexit.register(shutil.rmtree, self.home, ignore_errors=True)
        self._gpg('--quick-gen-key', uid, 'ed25519', 'sign', 'never')
        self.keyring = self.home / 'keyring.gpg'
        self.keyring.write_bytes(self._gpg('--export'))

    def inline(self, text: str) -> bytes:
        "An InRelease-style clearsigned document"
        return self._gpg('--clearsign', data=text.encode())

    def detached(self, data: bytes) -> bytes:
        "A Release.gpg-style detached binary signature"
        return self._gpg('--detach-sign', data=data)

    def _gpg(self, *args: str, data: bytes | None = None) -> bytes:
        proc = subprocess.run(
            [
                'gpg',
                '--homedir',
                str(self.home),
                '--batch',
                '--quiet',
                '--pinentry-mode',
                'loopback',
                '--passphrase',
                '',
                *args,
            ],
            input=data,
            capture_output=True,
            check=True,
        )
        return proc.stdout


@cache
def signer() -> Signer:
    "The shared test key"
    return Signer()


@cache
def other_signer() -> Signer:
    "A second key, trusted by nobody"
    return Signer('stranger <stranger@example.com>')


def stanza(
    name: str,
    version: str = '1.0',
    arch: str = 'amd64',
    content: bytes | None = None,
    **fields: str,
) -> tuple[str, bytes]:
    "A Packages stanza and the .deb bytes it describes"
    content = (
        content
        if content is not None
        else (f'{name} {version} {arch}'.encode())
    )
    filename = f'pool/main/{name[0]}/{name}/{name}_{version}_{arch}.deb'
    lines = [
        f'Package: {name}',
        f'Version: {version}',
        f'Architecture: {arch}',
    ]
    lines += [f'{k.replace("_", "-")}: {v}' for k, v in fields.items()]
    lines += [
        f'Filename: {filename}',
        f'Size: {len(content)}',
        f'MD5sum: {hashlib.md5(content).hexdigest()}',
        f'SHA256: {hashlib.sha256(content).hexdigest()}',
    ]
    return '\n'.join(lines) + '\n', content


def source_stanza(
    name: str,
    binary: str,
    version: str = '1.0',
    content: bytes | None = None,
    **fields: str,
) -> tuple[str, dict[str, bytes]]:
    "A Sources stanza and the {path: bytes} of the files it describes"
    content = (
        content if content is not None else (f'{name} {version} src'.encode())
    )
    directory = f'pool/main/{name[0]}/{name}'
    files = {
        f'{directory}/{name}_{version}.dsc': f'dsc {name} {version}'.encode(),
        f'{directory}/{name}_{version}.orig.tar.xz': content,
    }
    lines = [
        f'Package: {name}',
        f'Binary: {binary}',
        f'Version: {version}',
        f'Directory: {directory}',
    ]
    lines += [f'{k.replace("_", "-")}: {v}' for k, v in fields.items()]
    for field, digest in (
        ('Files', hashlib.md5),
        ('Checksums-Sha256', hashlib.sha256),
    ):
        lines.append(f'{field}:')
        for path, data in files.items():
            fname = path.rsplit('/', 1)[1]
            lines.append(f' {digest(data).hexdigest()} {len(data)} {fname}')
    return '\n'.join(lines) + '\n', files


class Archive:
    """A fake upstream: files by URL path, served by an httpx transport

    add_suite() writes Packages (.gz and .xz, the uncompressed one only
    listed), a translation, a Contents-amd64 file, and the signed InRelease /
    Release / Release.gpg.
    """

    def __init__(self, base: str = 'http://up.example') -> None:
        self.base = base
        self.files: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.by_hash = True

    def add_suite(
        self,
        root: str,
        suite: str,
        packages: dict[str, list[tuple[str, bytes]]],
        codename: str | None = None,
        signer_: Signer | None = None,
        inrelease: bool = True,
        release: bool = True,
        by_hash: bool = True,
        headers: dict[str, str] | None = None,
        extra: dict[str, bytes] | None = None,
        sources: list[tuple[str, dict[str, bytes]]] | None = None,
    ):
        """Publish a suite under root; packages maps component/arch dirs

        packages: {"main/binary-amd64": [stanza(...), ...]}
        extra: more index files by path, {"main/dep11/...": data}.
        headers: more Release fields, {"Origin": "Zabbix"}.
        sources: [source_stanza(...), ...], published as main/source/
        Sources; omit for an upstream that does not carry source at all.
        """
        signer_ = signer_ or signer()
        dists = f'{root}/dists/{suite}'
        listed: dict[str, bytes] = {}
        for head, entries in packages.items():
            text = '\n'.join(s for s, _ in entries).encode()
            listed[f'{head}/Packages'] = text
            for ext, data in (
                ('.gz', gzip.compress(text)),
                ('.xz', lzma.compress(text)),
            ):
                listed[f'{head}/Packages{ext}'] = data
            for s, content in entries:
                filename = next(
                    line.split(': ', 1)[1]
                    for line in s.splitlines()
                    if line.startswith('Filename: ')
                )
                self.files[f'{root}/{filename}'] = content
        if sources:
            text = '\n'.join(s for s, _ in sources).encode()
            listed['main/source/Sources'] = text
            for ext, data in (
                ('.gz', gzip.compress(text)),
                ('.xz', lzma.compress(text)),
            ):
                listed[f'main/source/Sources{ext}'] = data
            for _, files in sources:
                for path, content in files.items():
                    self.files[f'{root}/{path}'] = content
        listed['main/i18n/Translation-en.xz'] = lzma.compress(b'x')
        listed.update(extra or {})
        # mtime=0: the same bytes every time, so a re-published suite keeps
        # its Contents (and its by-hash name)
        listed['Contents-amd64.gz'] = gzip.compress(b'contents', mtime=0)
        sha = ['SHA256:']
        md5 = ['MD5Sum:']
        for path, data in sorted(listed.items()):
            sha.append(
                f' {hashlib.sha256(data).hexdigest()} {len(data)} {path}'
            )
            md5.append(f' {hashlib.md5(data).hexdigest()} {len(data)} {path}')
            if path.endswith('/Packages') or path.endswith('/Sources'):
                continue  # listed, not served (as Ubuntu does)
            self.files[f'{root}/dists/{suite}/{path}'] = data
            if by_hash:
                head = path.rpartition('/')[0]
                digest = hashlib.sha256(data).hexdigest()
                self.files[
                    f'{root}/dists/{suite}/{head}/by-hash/SHA256/{digest}'
                ] = data
        text = (
            '\n'.join(
                [
                    *(f'{k}: {v}' for k, v in (headers or {}).items()),
                    f'Suite: {suite}',
                    f'Codename: {codename or suite}',
                    'Date: Tue, 22 Sep 2026 08:00:00 UTC',
                    'Architectures: amd64 arm64',
                    'Components: main',
                    'Acquire-By-Hash: yes'
                    if by_hash
                    else 'Acquire-By-Hash: no',
                    *md5,
                    *sha,
                ]
            )
            + '\n'
        )
        if inrelease:
            self.files[f'{dists}/InRelease'] = signer_.inline(text)
        if release:
            self.files[f'{dists}/Release'] = text.encode()
            self.files[f'{dists}/Release.gpg'] = signer_.detached(
                text.encode()
            )
        return text

    def add_flat(
        self,
        root: str,
        packages: list[tuple[str, bytes]],
        by_hash: bool = False,
        signer_: Signer | None = None,
    ) -> str:
        """Publish a flat repository under root, as NVIDIA's CUDA ones are

        Release, InRelease, Release.gpg and Packages(.gz) sit at root with
        the .debs beside them, which Packages names ./<file>. The Release
        has no Suite, Codename or Components and a singular Architecture.
        """
        signer_ = signer_ or signer()
        lines = []
        for text, content in packages:
            stanza_lines = []
            for line in text.splitlines():
                if line.startswith('Filename: pool/main/'):
                    name = line.rsplit('/', 1)[1]
                    self.files[f'{root}/{name}'] = content
                    line = f'Filename: ./{name}'
                stanza_lines.append(line)
            lines.append('\n'.join(stanza_lines) + '\n')
        plain = '\n'.join(lines).encode()
        listed = {
            'Packages': plain,
            'Packages.gz': gzip.compress(plain, mtime=0),
        }
        sha = ['SHA256:']
        md5 = ['MD5Sum:']
        for path, data in sorted(listed.items()):
            digest = hashlib.sha256(data).hexdigest()
            sha.append(f' {digest} {len(data)} {path}')
            md5.append(f' {hashlib.md5(data).hexdigest()} {len(data)} {path}')
            self.files[f'{root}/{path}'] = data
            if by_hash and path != 'Packages':
                self.files[f'{root}/by-hash/SHA256/{digest}'] = data
        text = (
            '\n'.join(
                [
                    'Origin: Flat',
                    'Label: Flat',
                    'Architecture: x86_64',
                    'Date: Tue, 22 Sep 2026 08:00:00 UTC',
                    'Acquire-By-Hash: yes'
                    if by_hash
                    else 'Acquire-By-Hash: no',
                    *md5,
                    *sha,
                ]
            )
            + '\n'
        )
        self.files[f'{root}/InRelease'] = signer_.inline(text)
        self.files[f'{root}/Release'] = text.encode()
        self.files[f'{root}/Release.gpg'] = signer_.detached(text.encode())
        return text

    def transport(self) -> httpx.MockTransport:
        "An httpx transport serving the archive"

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            self.requests.append(path)
            if path in self.files:
                return httpx.Response(200, content=self.files[path])
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def client(self) -> httpx.Client:
        "An httpx client talking to this archive"
        return httpx.Client(transport=self.transport(), base_url=self.base)


class FakeStore:
    "The Store interface over a dict"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts: list[str] = []
        self.mtimes: dict[str, datetime] = {}
        self.multipart: set[str] = set()  # keys "uploaded" multipart
        # every list_etags/list_sizes call, as "method:prefix"; a full
        # mirror holds millions of keys, so a caller sharing one listing
        # instead of fetching its own shows up here as fewer calls
        self.listed: list[str] = []

    def _touch(self, key: str) -> None:
        self.mtimes[key] = datetime.now(timezone.utc)
        self.multipart.discard(key)

    def check(self) -> None:
        pass

    def list_objects(self, prefix: str) -> dict:
        now = datetime.now(timezone.utc)
        return {
            k: (len(v[0]), self.mtimes.get(k, now))
            for k, v in self.objects.items()
            if k.startswith(prefix)
        }

    def delete_many(self, keys: list[str]) -> None:
        for key in keys:
            self.delete(key)

    def list_etags(self, prefix: str) -> dict[str, tuple[int, str]]:
        self.listed.append(f'etags:{prefix}')
        out = {}
        for key, (data, _) in self.objects.items():
            if key.startswith(prefix):
                etag = hashlib.md5(data).hexdigest()
                if key in self.multipart:
                    etag += '-2'
                out[key] = (len(data), etag)
        return out

    def list_sizes(self, prefix: str) -> dict[str, int]:
        self.listed.append(f'sizes:{prefix}')
        return {
            k: len(v[0])
            for k, v in self.objects.items()
            if k.startswith(prefix)
        }

    def sha256(self, key: str) -> str | None:
        return self.objects[key][1] if key in self.objects else None

    def list_dirs(self, prefix: str) -> list[str]:
        return sorted(
            {
                k[len(prefix) :].split('/', 1)[0]
                for k in self.objects
                if k.startswith(prefix) and '/' in k[len(prefix) :]
            }
        )

    def get_bytes(self, key: str) -> bytes | None:
        return self.objects[key][0] if key in self.objects else None

    def put_bytes(self, key: str, data: bytes, sha256: str) -> None:
        self.puts.append(key)
        self.objects[key] = (data, sha256)
        self._touch(key)

    def put_file(
        self, key: str, path: Path, sha256: str, progress=None
    ) -> None:
        self.puts.append(key)
        self.objects[key] = (Path(path).read_bytes(), sha256)
        self._touch(key)

    def copy(self, src: str, dst: str) -> None:
        self.puts.append(dst)
        self.objects[dst] = self.objects[src]
        self._touch(dst)

    def delete(self, key: str) -> None:
        self.puts.append('-' + key)
        self.objects.pop(key, None)


DAY = date(2026, 9, 22)
UBUNTU = Upstream(
    name='ubuntu',
    keyring=signer().keyring,
    components={'noble': ('main',)},
    architectures=('amd64',),
    url='http://up.example/ubuntu',
    filter=Filter(('app',)),
    pool='_shared',
)
FLAT = Upstream(
    name='nvidia',
    keyring=signer().keyring,
    components={'nvidia': ()},
    architectures=(),
    url='http://up.example/cuda',
    flat=True,
    codename='noble',
)
K8S = Upstream(
    name='kubernetes',
    keyring=signer().keyring,
    components={'anydist': ('main',)},
    architectures=('amd64',),
    pool='_shared',
    channels={
        'verystable': Channel('1.30', 'http://up.example/k8s-1.30'),
        'stable': Channel('1.31', 'http://up.example/k8s-1.31'),
        'experimental': Channel('1.31', 'http://up.example/k8s-1.31'),
    },
)


class World:
    "An archive, its fetched suites, and a bucket with the pool synced"

    def __init__(self, tmp_path, app_version='1.0', **suite_kw):
        self.tmp_path = tmp_path
        self.archive = Archive()
        self.store = FakeStore()
        self.publish(app_version, **suite_kw)

    def publish(self, app_version, **suite_kw):
        self.archive.add_suite(
            '/ubuntu',
            'noble',
            **suite_kw,
            packages={
                'main/binary-amd64': [
                    stanza('app', app_version, Depends='libfoo'),
                    stanza('libfoo'),
                    stanza('unwanted'),
                ]
            },
        )
        for tree in ('1.30', '1.31'):
            self.archive.add_suite(
                f'/k8s-{tree}',
                'anydist',
                {'main/binary-amd64': [stanza('kubelet', f'{tree}.0')]},
            )
        http = self.archive.client()
        self.suites = [
            fetch_suite(http, up, source, suite, self.tmp_path / 'scratch')
            for up in (UBUNTU, K8S)
            for source in up.sources()
            for suite in up.suites
        ]
        self.selection = select(self.suites)

    def sync_pool(self):
        sync(
            self.selection,
            self.store,
            self.archive.client(),
            self.tmp_path / 'tmp',
            progress=False,
        )

    def suite(self, name, tree=None):
        return next(
            fs
            for fs in self.suites
            if fs.suite == name and fs.source.tree == tree
        )

    def cut(self, fs, **kw):
        kw.setdefault('today', DAY)
        return cut(
            fs, self.selection, self.store, self.store.list_sizes(PREFIX), **kw
        )


UP = replace(
    UBUNTU, components={'noble': ('main',), 'noble-updates': ('main',)}
)


def two_suites(tmp_path) -> World:
    "noble and noble-updates cut and recorded in ubuntu/acc.yaml"
    w = World(tmp_path)
    w.archive.add_suite(
        '/ubuntu',
        'noble-updates',
        {'main/binary-amd64': [stanza('app', '1.0.1', Depends='libfoo')]},
        codename='noble',
    )
    http = w.archive.client()
    w.suites = [
        fetch_suite(http, up, src, suite, tmp_path / 'scratch')
        for up in (UP, K8S)
        for src in up.sources()
        for suite in up.suites
    ]
    w.selection = select(w.suites)
    w.sync_pool()
    for suite in ('noble', 'noble-updates'):
        record(w.cut(w.suite(suite)), tmp_path / 'manifests', 'bkt')
    return w


def move_noble(w, version='2.0'):
    "Publish, fetch, sync and cut a new noble; recorded in acc.yaml"
    w.archive.add_suite(
        '/ubuntu',
        'noble',
        {
            'main/binary-amd64': [
                stanza('app', version, Depends='libfoo'),
                stanza('libfoo'),
            ]
        },
    )
    w.suites[0] = fetch_suite(
        w.archive.client(),
        UP,
        UP.sources()[0],
        'noble',
        w.tmp_path / 'scratch',
    )
    w.selection = select(w.suites)
    w.sync_pool()
    record(w.cut(w.suite('noble')), w.tmp_path / 'manifests', 'bkt')


UPG = replace(UP, group='ubuntu')
SEC = replace(
    UBUNTU,
    name='ubuntu-security',
    components={'noble-security': ('main',)},
    group='ubuntu',
)


def group_world(tmp_path, apply_security=True) -> World:
    "ubuntu (noble, noble-updates) and ubuntu-security, acc applied"
    w = two_suites(tmp_path)
    w.archive.add_suite(
        '/ubuntu',
        'noble-security',
        {'main/binary-amd64': [stanza('app', '1.0.2', Depends='libfoo')]},
        codename='noble',
    )
    sec = fetch_suite(
        w.archive.client(),
        SEC,
        SEC.sources()[0],
        'noble-security',
        tmp_path / 'scratch',
    )
    w.suites.append(sec)
    w.selection = select(w.suites)
    w.sync_pool()
    record(w.cut(sec), tmp_path / 'manifests', 'bkt')
    for up in (UP, SEC) if apply_security else (UP,):
        acc = Manifest.load(
            tmp_path / 'manifests', 'bkt', up.name, up.served, None, 'acc'
        )
        run(build(acc, up, w.store), w.store)
    return w
