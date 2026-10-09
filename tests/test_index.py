import gzip
import lzma

from aptberg import index
from aptberg.release import Entry, Release


def _release(*paths: str) -> Release:
    return Release(
        'noble',
        'noble',
        '',
        ('main',),
        ('amd64',),
        True,
        {p: Entry(p, '0' * 64, 1) for p in paths},
    )


def test_selected():
    "Configured binary dirs, binary-all, i18n and extras; compressed only"
    rel = _release(
        'main/binary-amd64/Packages',
        'main/binary-amd64/Packages.gz',
        'main/binary-amd64/Packages.xz',
        'main/binary-amd64/Release',
        'main/binary-all/Packages',  # only form: kept
        'main/binary-arm64/Packages.xz',  # arch not configured
        'main/i18n/Translation-en.xz',
        'main/dep11/Components-amd64.yml.gz',
        'universe/binary-amd64/Packages.xz',  # component not configured
        'Contents-amd64.gz',
    )
    paths = [e.path for e in index.selected(rel, ['main'], ['amd64'])]
    assert paths == [
        'Contents-amd64.gz',
        'main/binary-all/Packages',
        'main/binary-amd64/Packages.gz',
        'main/binary-amd64/Packages.xz',
        'main/binary-amd64/Release',
        'main/dep11/Components-amd64.yml.gz',
        'main/i18n/Translation-en.xz',
    ]


def test_selected_contents():
    "Contents-<arch> is taken, top-level or per-component"
    rel = _release(
        'main/binary-amd64/Packages.xz',
        'Contents-amd64.gz',  # Ubuntu-style, top-level
        'Contents-amd64',  # only kept if no compressed form
        'Contents-arm64.gz',  # arch not configured
        'main/Contents-amd64.gz',  # per-component, e.g. Debian-style
        'universe/Contents-amd64.gz',  # component not configured
    )
    paths = [e.path for e in index.selected(rel, ['main'], ['amd64'])]
    assert paths == [
        'Contents-amd64.gz',
        'main/Contents-amd64.gz',
        'main/binary-amd64/Packages.xz',
    ]


def test_selected_source():
    "source/ is included by default, dropped when source=False"
    rel = _release(
        'main/binary-amd64/Packages.xz',
        'main/source/Sources.gz',
        'main/source/Sources.xz',
        'universe/source/Sources.xz',  # component not configured
    )
    with_source = [e.path for e in index.selected(rel, ['main'], ['amd64'])]
    assert with_source == [
        'main/binary-amd64/Packages.xz',
        'main/source/Sources.gz',
        'main/source/Sources.xz',
    ]
    without = index.selected(rel, ['main'], ['amd64'], source=False)
    assert [e.path for e in without] == ['main/binary-amd64/Packages.xz']


def test_selected_source_absent_is_not_an_error():
    "An upstream that does not publish source/ contributes nothing"
    rel = _release('main/binary-amd64/Packages.xz')
    assert [e.path for e in index.selected(rel, ['main'], ['amd64'])] == [
        'main/binary-amd64/Packages.xz'
    ]


def test_packages_indexes_prefers_xz():
    "One Packages file per binary dir, best compression first"
    rel = _release(
        'main/binary-amd64/Packages.gz',
        'main/binary-amd64/Packages.xz',
        'main/binary-all/Packages',
        'main/binary-amd64/Release',
    )
    picked = index.packages_indexes(rel.entries.values())
    assert {k: v.path for k, v in picked.items()} == {
        'main/binary-amd64': 'main/binary-amd64/Packages.xz',
        'main/binary-all': 'main/binary-all/Packages',
    }


def test_sources_indexes_prefers_xz():
    "One Sources file per source dir, best compression first"
    rel = _release(
        'main/source/Sources.gz',
        'main/source/Sources.xz',
        'main/binary-amd64/Packages.xz',
    )
    picked = index.sources_indexes(rel.entries.values())
    assert {k: v.path for k, v in picked.items()} == {
        'main/source': 'main/source/Sources.xz'
    }


def test_source_files():
    "Directory joined with each Checksums-Sha256 line"
    stanza = {
        'Package': 'app',
        'Directory': 'pool/main/a/app',
        'Checksums-Sha256': (
            'abc123 100 app_1.0.dsc\ndef456 2000 app_1.0.orig.tar.gz'
        ),
    }
    assert index.source_files(stanza) == [
        ('pool/main/a/app/app_1.0.dsc', 100, 'abc123'),
        ('pool/main/a/app/app_1.0.orig.tar.gz', 2000, 'def456'),
    ]


def test_source_files_missing_fields_is_not_an_error():
    "No Directory, no Checksums-Sha256, or a malformed line: skipped"
    assert index.source_files({'Package': 'app'}) == []
    assert index.source_files(
        {
            'Package': 'app',
            'Directory': 'pool/main/a/app',
            'Checksums-Sha256': 'not a checksum line\nabc123 100 ok.dsc',
        }
    ) == [('pool/main/a/app/ok.dsc', 100, 'abc123')]


def test_by_hash_dir():
    "No leading or doubled slash, whether path has a directory or not"
    assert (
        index.by_hash_dir('main/binary-amd64/Packages.xz')
        == 'main/binary-amd64/by-hash/SHA256'
    )
    assert index.by_hash_dir('Contents-amd64.gz') == 'by-hash/SHA256'


def test_open_text(tmp_path):
    "Compressed and plain indexes read the same, from a file or bytes"
    for name, data in (
        ('P', b'a\n'),
        ('P.gz', gzip.compress(b'a\n')),
        ('P.xz', lzma.compress(b'a\n')),
    ):
        (tmp_path / name).write_bytes(data)
        with index.open_text(tmp_path / name) as fh:
            assert fh.read() == 'a\n'
        with index.open_text(name, data) as fh:
            assert fh.read() == 'a\n'
