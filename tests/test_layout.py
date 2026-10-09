from dataclasses import replace

from aptberg.layout import dists, listing_root, owner

from .helpers import FLAT, UBUNTU

OLD = replace(UBUNTU, name='ubuntu-old', components={'precise': ('main',)})


def test_dists():
    "dists/<suite>/ below the prefix; a flat upstream at the prefix itself"
    assert dists(UBUNTU, 'ubuntu/ch/acc/', 'noble') == (
        'ubuntu/ch/acc/dists/noble/'
    )
    assert dists(FLAT, 'nvidia/ch/acc/', 'nvidia') == 'nvidia/ch/acc/'


def test_listing_root():
    "dists/ holds everything served, unless the upstream is flat"
    assert listing_root([UBUNTU, OLD], 'u/ch/acc/') == 'u/ch/acc/dists/'
    assert listing_root([FLAT], 'n/ch/acc/') == 'n/ch/acc/'


def test_owner():
    "The upstream listing the suite; the sole one; else nobody"
    shared = {'ubuntu': UBUNTU, 'ubuntu-old': OLD}
    assert owner(shared, 'noble') is UBUNTU
    assert owner(shared, 'precise') is OLD
    assert owner(shared, 'jammy') is None  # dropped, but by which one?
    assert owner({'ubuntu': UBUNTU}, 'jammy') is UBUNTU
