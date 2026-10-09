import pytest

from aptberg.release import Release, SignatureError, family, verified_payload

from .helpers import other_signer, signer

RELEASE = """\
Origin: Ubuntu
Suite: noble-updates
Codename: noble
Date: Tue, 22 Sep 2026  7:49:53 UTC
Architectures: amd64 arm64
Components: main restricted universe
Acquire-By-Hash: yes
MD5Sum:
 0123 99 main/binary-amd64/Packages.gz
SHA256:
 aaaa 12 main/binary-amd64/Packages
 bbbb 34 main/binary-amd64/Packages.gz
"""


def test_parse():
    "The fields aptberg acts on, entries from the SHA256 block only"
    rel = Release.parse(RELEASE)
    assert (rel.suite, rel.codename) == ('noble-updates', 'noble')
    assert rel.components == ('main', 'restricted', 'universe')
    assert rel.architectures == ('amd64', 'arm64')
    assert rel.acquire_by_hash
    assert sorted(rel.entries) == [
        'main/binary-amd64/Packages',
        'main/binary-amd64/Packages.gz',
    ]
    entry = rel.entries['main/binary-amd64/Packages.gz']
    assert (entry.sha256, entry.size, entry.md5) == ('bbbb', 34, '0123')
    assert rel.entries['main/binary-amd64/Packages'].md5 == ''


def test_parse_requires_sha256():
    "A Release with only MD5Sum is refused"
    with pytest.raises(ValueError):
        Release.parse(RELEASE.split('SHA256:')[0])


def test_inline_good(tmp_path):
    "An InRelease signed by the pinned key yields its payload"
    path = tmp_path / 'InRelease'
    path.write_bytes(signer().inline(RELEASE))
    assert verified_payload(path, signer().keyring) == RELEASE


def test_inline_tampered(tmp_path):
    "One changed byte in the signed text fails verification"
    signed = signer().inline(RELEASE).replace(b'aaaa 12', b'aaaa 13')
    path = tmp_path / 'InRelease'
    path.write_bytes(signed)
    with pytest.raises(SignatureError):
        verified_payload(path, signer().keyring)


def test_inline_unsigned_prefix_not_returned(tmp_path):
    "Text outside the signed region never reaches the caller"
    path = tmp_path / 'InRelease'
    path.write_bytes(b'Suite: evil\n\n' + signer().inline(RELEASE))
    payload = verified_payload(path, signer().keyring)
    assert 'evil' not in payload


def test_wrong_key(tmp_path):
    "A good signature from a key outside the keyring is refused"
    path = tmp_path / 'InRelease'
    path.write_bytes(other_signer().inline(RELEASE))
    with pytest.raises(SignatureError):
        verified_payload(path, signer().keyring)


def test_detached(tmp_path):
    "Release plus Release.gpg yields the Release bytes"
    rel, sig = tmp_path / 'Release', tmp_path / 'Release.gpg'
    rel.write_text(RELEASE)
    sig.write_bytes(signer().detached(RELEASE.encode()))
    assert verified_payload(rel, signer().keyring, sig) == RELEASE


def test_detached_tampered(tmp_path):
    "A Release changed after signing is refused"
    rel, sig = tmp_path / 'Release', tmp_path / 'Release.gpg'
    sig.write_bytes(signer().detached(RELEASE.encode()))
    rel.write_text(RELEASE.replace('bbbb', 'cccc'))
    with pytest.raises(SignatureError):
        verified_payload(rel, signer().keyring, sig)


def test_multisig_good_and_unknown_accepted(tmp_path):
    "A valid signature plus one from a key outside the keyring is fine"
    rel, sig = tmp_path / 'Release', tmp_path / 'Release.gpg'
    rel.write_text(RELEASE)
    sig.write_bytes(
        signer().detached(RELEASE.encode())
        + other_signer().detached(RELEASE.encode())
    )
    assert verified_payload(rel, signer().keyring, sig) == RELEASE


def test_multisig_good_and_bad_refused(tmp_path):
    "A bad signature is refused even alongside a valid one"
    rel, sig = tmp_path / 'Release', tmp_path / 'Release.gpg'
    rel.write_text(RELEASE)
    other = RELEASE.replace('bbbb', 'cccc')
    sig.write_bytes(
        signer().detached(RELEASE.encode()) + signer().detached(other.encode())
    )
    with pytest.raises(SignatureError):
        verified_payload(rel, signer().keyring, sig)


def test_multisig_unknown_only_refused(tmp_path):
    "A signature from a key outside the keyring, alone, is refused"
    rel, sig = tmp_path / 'Release', tmp_path / 'Release.gpg'
    rel.write_text(RELEASE)
    sig.write_bytes(other_signer().detached(RELEASE.encode()))
    with pytest.raises(SignatureError):
        verified_payload(rel, signer().keyring, sig)


def test_family():
    "Debian's pocket codenames fold into their release; others stay"
    assert family('bookworm-security') == 'bookworm'
    assert family('trixie-backports') == 'trixie'
    assert family('bookworm-updates') == 'bookworm'
    assert family('bookworm') == 'bookworm'
    assert family('noble') == 'noble'
    assert family('anydist') == 'anydist'
