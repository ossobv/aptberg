import pytest

from aptberg.manifest import (
    AUTO_STAGES,
    STAGES,
    Manifest,
    ManifestError,
    Ref,
    prefix_name,
)


def test_stages():
    "cur is a stage like acc and prod; cur and acc are auto-recorded"
    assert STAGES == ('cur', 'acc', 'prod')
    assert AUTO_STAGES == ('cur', 'acc')
    assert prefix_name(None, 'cur') == 'cur'
    assert prefix_name('stable', 'cur') == 'stable-cur'
    with pytest.raises(ManifestError, match='unknown stage'):
        prefix_name(None, 'bogus')


def test_ref():
    "Bare ids and tree-qualified ids round-trip"
    assert Ref.parse('20260909a') == Ref(None, '20260909a')
    assert Ref.parse('1.31/20260101b') == Ref('1.31', '20260101b')
    assert str(Ref('1.31', '20260101b')) == '1.31/20260101b'
    for bad in ('2026090a', '20260909', '20260909A', '/20260909a', ''):
        with pytest.raises(ManifestError):
            Ref.parse(bad)


def test_roundtrip(tmp_path):
    "A missing file is empty; saved files are sorted and reload equal"
    m = Manifest.load(
        tmp_path, 'bkt', 'kubernetes', 'kubernetes', 'stable', 'acc'
    )
    assert m.path == tmp_path / 'kubernetes' / 'stable-acc.yaml'
    assert m.prefix == 's3://bkt/kubernetes/ch/stable-acc'
    assert m.suites == {}
    m.suites['noble-updates'] = Ref(None, '20260901b')
    m.suites['jammy'] = Ref(None, '20260715a')
    m.save()
    assert m.path.read_text() == (
        'prefix: s3://bkt/kubernetes/ch/stable-acc\n'
        'suites:\n'
        '  jammy:         20260715a\n'
        '  noble-updates: 20260901b\n'
    )
    again = Manifest.load(
        tmp_path, 'bkt', 'kubernetes', 'kubernetes', 'stable', 'acc'
    )
    assert again.suites == m.suites


def test_served_path_differs_from_name(tmp_path):
    "path: an upstream is served under another upstream's name"
    m = Manifest.load(tmp_path, 'bkt', 'debian-old', 'debian', None, 'acc')
    assert m.path == tmp_path / 'debian-old' / 'acc.yaml'
    assert m.prefix == 's3://bkt/debian/ch/acc'
    m.suites['jessie'] = Ref(None, '20260101a')
    m.save()
    again = Manifest.read(m.path, 'bkt', 'debian')
    assert again.suites == m.suites
    assert again.upstream == 'debian-old'


def test_prefix_mismatch(tmp_path):
    "A manifest for another prefix is refused"
    path = tmp_path / 'ubuntu' / 'acc.yaml'
    path.parent.mkdir()
    path.write_text('prefix: s3://bkt/ubuntu/ch/prod\nsuites: {}\n')
    with pytest.raises(ManifestError, match='expected'):
        Manifest.load(tmp_path, 'bkt', 'ubuntu', 'ubuntu', None, 'acc')
