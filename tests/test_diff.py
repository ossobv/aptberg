import pytest

from aptberg.diff import DiffError, diff_suite
from aptberg.fetch import fetch_suite
from aptberg.manifest import Ref
from aptberg.pool import select

from .helpers import UP, World, stanza


def _recut_noble(w, packages):
    "Republish noble's content, refetch, resync and cut it again"
    w.archive.add_suite('/ubuntu', 'noble', {'main/binary-amd64': packages})
    w.suites[0] = fetch_suite(
        w.archive.client(),
        UP,
        UP.sources()[0],
        'noble',
        w.tmp_path / 'scratch',
    )
    w.selection = select(w.suites)
    w.sync_pool()
    return w.cut(w.suite('noble'))


def test_diff_added_removed_changed(tmp_path):
    "Package added, one removed, one with a bumped version"
    w = World(tmp_path)
    w.sync_pool()
    ref_a = w.cut(w.suite('noble')).ref
    assert ref_a == Ref(None, '20260922a')

    ref_b = _recut_noble(
        w,
        [
            stanza('app', '2.0', Depends='libfoo'),
            stanza('libfoo'),
            stanza('newpkg'),
        ],
    ).ref
    assert ref_b == Ref(None, '20260922b')

    result = diff_suite(w.store, 'ubuntu', 'noble', ref_a, ref_b)
    assert list(result) == ['main/binary-amd64']
    d = result['main/binary-amd64']
    assert d.added == {'newpkg': '1.0'}
    assert d.removed == {'unwanted': '1.0'}
    assert d.changed == {'app': ('1.0', '2.0')}


def test_diff_no_changes(tmp_path):
    "An unchanged suite reuses its id and diffs empty against itself"
    w = World(tmp_path)
    w.sync_pool()
    ref_a = w.cut(w.suite('noble')).ref
    result = w.cut(w.suite('noble'))
    assert result.reused and result.ref == ref_a
    assert diff_suite(w.store, 'ubuntu', 'noble', ref_a, result.ref) == {}


def test_diff_missing_snapshot(tmp_path):
    "A snapshot id that was never cut is refused, not silently empty"
    w = World(tmp_path)
    w.sync_pool()
    ref_a = w.cut(w.suite('noble')).ref
    with pytest.raises(DiffError, match='no such snapshot'):
        diff_suite(w.store, 'ubuntu', 'noble', ref_a, Ref(None, '20260101a'))
