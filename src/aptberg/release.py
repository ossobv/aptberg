"""Release and InRelease: signature verification and parsing

The text handed to the parser is always what gpgv vouched for, never the
file on disk: anything outside the signed region of an InRelease must not
reach the parser.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .deb822 import stanzas


class SignatureError(Exception):
    "A signature did not verify against the pinned keyring"


def verified_payload(
    signed: Path, keyring: Path, detached: Path | None = None
) -> str:
    """The signed text of an InRelease, or of a Release with its Release.gpg

    gpgv runs with an empty home directory, so a trustedkeys.kbx of whoever
    runs aptberg cannot widen the pinned keyring. For the detached form the
    bytes are read once and fed to gpgv on stdin, so the text returned is
    exactly the text verified.

    Accepted whenever at least one signature is valid against the keyring:
    a co-signature from a key an archive has only just started using, and
    that aptberg's keyring does not carry yet, must not fail the whole
    suite. A signature that fails against a key gpgv does know (BADSIG)
    still rejects the file even when another signature is good, so this
    is not the same as ignoring gpgv's exit code outright.
    """
    with TemporaryDirectory(prefix='aptberg-gpgv-') as home:
        status_path = Path(home, 'status')
        with status_path.open('wb') as status_fh:
            cmd = [
                'gpgv',
                '--homedir',
                home,
                '--status-fd',
                str(status_fh.fileno()),
                '--keyring',
                str(Path(keyring).resolve()),
            ]
            if detached is None:
                data = None
                cmd += ['--output', '-', str(signed)]
            else:
                data = Path(signed).read_bytes()
                cmd += [str(detached), '-']
            proc = subprocess.run(
                cmd,
                input=data,
                capture_output=True,
                pass_fds=(status_fh.fileno(),),
            )
        codes = [
            line.split()[1]
            for line in status_path.read_text(errors='replace').splitlines()
            if line.startswith('[GNUPG:] ')
        ]
    if 'BADSIG' in codes or 'VALIDSIG' not in codes:
        err = proc.stderr.decode(errors='replace').strip()
        raise SignatureError(f'{signed}: {err}')
    return (proc.stdout if data is None else data).decode()


def verified_release(files: dict[str, bytes], keyring: Path) -> str:
    """The payload of a suite's signature files, every form verified

    files maps InRelease, Release and Release.gpg (whichever exist) to
    their bytes. Every form present must verify, since every form is
    published and some clients use each, and they must agree.
    """
    with TemporaryDirectory(prefix='aptberg-release-') as tmp:
        paths = {}
        for name, data in files.items():
            paths[name] = Path(tmp, name)
            paths[name].write_bytes(data)
        payloads = set()
        if 'InRelease' in paths:
            payloads.add(verified_payload(paths['InRelease'], keyring))
        if 'Release' in paths or 'Release.gpg' in paths:
            if not ('Release' in paths and 'Release.gpg' in paths):
                raise SignatureError(
                    'Release without Release.gpg or the other way around'
                )
            payloads.add(
                verified_payload(
                    paths['Release'], keyring, paths['Release.gpg']
                )
            )
    if len(payloads) != 1:
        raise SignatureError('no signature, or InRelease and Release disagree')
    return payloads.pop()


@dataclass(frozen=True, slots=True)
class Entry:
    "One line of the SHA256 block: a file relative to dists/<suite>/"

    path: str
    sha256: str
    size: int
    md5: str = ''  # from the MD5Sum block, when the Release has one


# Debian names the Codename: of its pocket suites after the pocket
# (bookworm-security, bookworm-backports), where Ubuntu keeps one codename
# for all of them.
_POCKETS = ('-proposed-updates', '-backports', '-updates', '-security')


def family(codename: str) -> str:
    """The release a Release codename belongs to

    "bookworm-security" -> "bookworm", "noble" -> "noble". The pockets
    of a release are built on it (a security update depends on the base
    release's libraries), so they resolve dependencies together.
    """
    for pocket in _POCKETS:
        if codename.endswith(pocket):
            return codename[: -len(pocket)]
    return codename


@dataclass(frozen=True)
class Release:
    "The fields of a verified Release payload that aptberg acts on"

    suite: str
    codename: str
    date: str
    components: tuple[str, ...]
    architectures: tuple[str, ...]
    acquire_by_hash: bool
    entries: dict[str, Entry]
    origin: str = ''
    label: str = ''

    @classmethod
    def parse(cls, text: str) -> 'Release':
        "Parse a verified payload; a Release without SHA256 is refused"
        fields = next(stanzas(text.splitlines()), {})
        if 'SHA256' not in fields:
            raise ValueError('Release has no SHA256 block')
        md5s = {}
        for line in fields.get('MD5Sum', '').splitlines():
            if line.strip():
                md5, _, path = line.split()
                md5s[path] = md5
        entries = {}
        for line in fields['SHA256'].splitlines():
            if not line.strip():
                continue
            sha256, size, path = line.split()
            entries[path] = Entry(path, sha256, int(size), md5s.get(path, ''))
        return cls(
            suite=fields.get('Suite', ''),
            codename=fields.get('Codename', ''),
            date=fields.get('Date', ''),
            components=tuple(fields.get('Components', '').split()),
            architectures=tuple(fields.get('Architectures', '').split()),
            acquire_by_hash=(
                fields.get('Acquire-By-Hash', '').lower() == 'yes'
            ),
            entries=entries,
            origin=fields.get('Origin', ''),
            label=fields.get('Label', ''),
        )
