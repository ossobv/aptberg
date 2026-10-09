"""Which index files of a Release we mirror, and how to read them

We mirror the binary Packages indexes of the configured components and
architectures (plus binary-all), the i18n translations, and, unless an
upstream turns it off, each component's source Sources index (for
deb-src). The extra index directories in DIR_EXTRA are always added
too: dep11 (AppStream, which apt fetches on hosts with appstream
installed) and cnf (command-not-found), as is Contents-<arch>, which
apt (apt-file 3.x) fetches on every `apt update` for every
architecture it tracks, and fails the repo on when the Release lists
it but we do not serve it. Contents is not directory-shaped like the
others -- Ubuntu publishes it at the top of dists/<suite>/, other
archives per component -- so it is matched on name, not path prefix,
in both places. A component an upstream does not publish source for
simply contributes no source/ entries -- never an error.
"""

import bz2
import gzip
import io
import lzma
from collections.abc import Iterable
from typing import TextIO

from .config import Upstream
from .release import Entry, Release

OPENERS = {'.xz': lzma.open, '.gz': gzip.open, '.bz2': bz2.open}
COMPRESSED = ('.xz', '.gz', '.bz2', '.lzma', '.zst')
# Preferred order when we parse an index ourselves; the empty suffix is
# the uncompressed file.
PARSE_ORDER = ('.xz', '.gz', '.bz2', '')
# Index directories next to binary-<arch>/ that an upstream may add.
DIR_EXTRA = ('cnf', 'dep11')


def optional(path: str) -> bool:
    """Whether upstream may list path in its Release without serving it

    Old archives (Debian jessie on archive.debian.org) list
    <component>/binary-all/ files in the Release but never published
    them: their architecture-independent packages live in binary-<arch>.
    """
    return '/binary-all/' in path


def split_compression(path: str) -> tuple[str, str]:
    "Split a path into its base and compression suffix (maybe empty)"
    for ext in COMPRESSED:
        if path.endswith(ext):
            return path[: -len(ext)], ext
    return path, ''


def selected(
    release: Release,
    components: Iterable[str],
    architectures: Iterable[str],
    source: bool = True,
) -> list[Entry]:
    """The Release entries we fetch and snapshot, sorted by path

    Ubuntu lists uncompressed indexes in its Release but does not serve
    them, and apt never asks for them when a compressed variant exists.
    So an uncompressed entry is only selected when it is the only form.

    In the directory-shaped extras (dep11, cnf) files are per
    architecture (Components-amd64.yml.gz, Commands-arm64.xz) or for
    all (icons-*): only those of the configured architectures are
    taken. Contents-<arch> is taken for the configured architectures,
    wherever the Release lists it: at the top of dists/<suite>/
    (Ubuntu) or under a component (most other archives). Unlike
    Packages there is no Contents-all.

    source adds each component's source/ directory (Sources), for
    deb-src; a component the Release does not list one for contributes
    nothing.
    """
    components = tuple(components)
    architectures = tuple(architectures)
    dirs = tuple(
        f'{c}/binary-{a}/' for c in components for a in (*architectures, 'all')
    )
    dirs += tuple(f'{c}/i18n/' for c in components)
    if source:
        dirs += tuple(f'{c}/source/' for c in components)
    extra_dirs = tuple(f'{c}/{x}/' for c in components for x in DIR_EXTRA)
    others = [
        a
        for a in release.architectures
        if a not in architectures and a != 'all'
    ]
    contents = {f'Contents-{a}' for a in architectures}
    contents |= {
        f'{c}/Contents-{a}' for c in components for a in architectures
    }
    paths = {
        p
        for p in release.entries
        if p.startswith(dirs)
        or (p.startswith(extra_dirs) and not _for_arch(p, others))
        or (split_compression(p)[0] in contents)
    }
    out = []
    for path in paths:
        base, ext = split_compression(path)
        if not ext and any(base + e in paths for e in COMPRESSED):
            continue
        out.append(release.entries[path])
    return sorted(out, key=lambda e: e.path)


def flat_selected(release: Release, source: bool = True) -> list[Entry]:
    """The Release entries of a flat repository, sorted by path

    A flat Release lists only what apt fetches from the repository root,
    so everything it lists is taken, apart from the uncompressed form
    of a file with a compressed one (as in selected()) and, unless
    source, the Sources indexes.
    """
    paths = set(release.entries)
    out = []
    for path in sorted(paths):
        base, ext = split_compression(path)
        if not ext and any(base + e in paths for e in COMPRESSED):
            continue
        if not source and base.rpartition('/')[2] == 'Sources':
            continue
        out.append(release.entries[path])
    return out


def wanted(release: Release, upstream: Upstream, suite: str) -> list[Entry]:
    "The Release entries we mirror of a suite of this upstream"
    if upstream.flat:
        return flat_selected(release, upstream.deb_src)
    return selected(
        release,
        upstream.components[suite],
        upstream.architectures,
        source=upstream.deb_src,
    )


def _for_arch(path: str, archs: Iterable[str]) -> bool:
    "Whether a file name is marked as for one of archs: Foo-amd64.yml"
    name, _ = split_compression(path.rpartition('/')[2])
    return any(name.endswith(f'-{a}') or f'-{a}.' in name for a in archs)


def _stem_indexes(entries: Iterable[Entry], stem: str) -> dict[str, Entry]:
    "Map each directory to its <stem> file we parse, best compression first"
    found: dict[str, dict[str, Entry]] = {}
    for entry in entries:
        base, ext = split_compression(entry.path)
        head, _, name = base.rpartition('/')
        if name == stem and ext in PARSE_ORDER:
            found.setdefault(head, {})[ext] = entry
    return {
        head: next(byext[e] for e in PARSE_ORDER if e in byext)
        for head, byext in found.items()
        if any(e in byext for e in PARSE_ORDER)
    }


def packages_indexes(entries: Iterable[Entry]) -> dict[str, Entry]:
    """Map each binary-<arch> directory to the Packages file we parse

    For example "main/binary-amd64" -> the Packages.xz entry.
    """
    return _stem_indexes(entries, 'Packages')


def sources_indexes(entries: Iterable[Entry]) -> dict[str, Entry]:
    """Map each source directory to the Sources file we parse

    For example "main/source" -> the Sources.xz entry.
    """
    return _stem_indexes(entries, 'Sources')


def source_files(stanza: dict[str, str]) -> list[tuple[str, int, str]]:
    """The (filename, size, sha256) of every file a Sources stanza names

    Reads Directory: and Checksums-Sha256: (one "sha256 size name" line
    per file, deb822.stanzas joins them with a leading space stripped).
    A stanza lacking either field, or a line that does not parse,
    contributes nothing rather than raising: a source package we cannot
    place a file for is simply not mirrored, not a hard failure.
    """
    directory = stanza.get('Directory', '').strip()
    checksums = stanza.get('Checksums-Sha256', '')
    if not directory or not checksums:
        return []
    out = []
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        sha256, size, name = parts
        out.append((f'{directory}/{name}', int(size), sha256))
    return out


def source_md5s(stanza: dict[str, str]) -> dict[str, str]:
    """filename -> md5 of every file a Sources stanza names

    From Files: ("md5 size name" lines), placed like source_files does.
    Missing or unparsable: nothing, and the file is checked another way.
    """
    directory = stanza.get('Directory', '').strip()
    out = {}
    for line in stanza.get('Files', '').splitlines():
        parts = line.split()
        if directory and len(parts) == 3:
            out[f'{directory}/{parts[2]}'] = parts[0]
    return out


def by_hash_dir(path: str) -> str:
    """path's by-hash/SHA256 directory, relative to the same base as path

    A path with no directory of its own (Contents-<arch> at the top of
    dists/<suite>/) yields "by-hash/SHA256" with no leading slash, so a
    caller joining it with "/" between base and this, and "/" between
    this and the hash, never produces a doubled slash either side.
    """
    head = path.rpartition('/')[0]
    return f'{head}/by-hash/SHA256' if head else 'by-hash/SHA256'


def open_text(path, data: bytes | None = None) -> TextIO:
    """Open a possibly compressed index as a text stream

    From the file at path, or, given data, from those bytes; path then
    only says how they are compressed.
    """
    _, ext = split_compression(str(path))
    opener = OPENERS.get(ext)
    if opener is None and ext:
        raise ValueError(f'{path}: cannot decompress {ext}')
    source = path if data is None else io.BytesIO(data)
    if opener is not None:
        return opener(source, 'rt', encoding='utf-8')
    if data is None:
        return open(path, encoding='utf-8')
    return io.TextIOWrapper(source, encoding='utf-8')
