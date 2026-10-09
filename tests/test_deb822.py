import gzip
import io

import pytest

from aptberg.deb822 import stanzas

PACKAGES = """\
Package: openssh-server
Architecture: amd64
Version: 1:9.6p1-3ubuntu13.5
Depends: libc6 (>= 2.38), openssh-client (= 1:9.6p1-3ubuntu13.5)
Filename: pool/main/o/openssh/openssh-server_9.6p1-3ubuntu13.5_amd64.deb
Size: 510412
SHA256: 0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0
Description: secure shell (SSH) server
 This is the portable version of OpenSSH.
 .
 It provides the sshd daemon.

Package: zlib1g
Architecture: amd64
Version: 1:1.3.dfsg-3.1ubuntu2.1
Filename: pool/main/z/zlib/zlib1g_1.3.dfsg-3.1ubuntu2.1_amd64.deb
"""


def test_stanzas_fields_and_continuations():
    "Fields split on the first colon, continuations join with newline"
    first, second = stanzas(io.StringIO(PACKAGES))
    assert first['Package'] == 'openssh-server'
    assert first['Version'] == '1:9.6p1-3ubuntu13.5'
    assert first['Description'] == (
        'secure shell (SSH) server\n'
        'This is the portable version of OpenSSH.\n'
        '.\n'
        'It provides the sshd daemon.'
    )
    assert second['Filename'].endswith('_amd64.deb')


def test_stanzas_empty_first_line_value():
    "A field whose value starts on the next line keeps an empty head"
    (release,) = stanzas(
        [
            'Codename: noble\n',
            'SHA256:\n',
            ' abc 12 main/binary-amd64/Packages\n',
            ' def 34 main/binary-amd64/Packages.gz\n',
        ]
    )
    assert release['SHA256'] == (
        '\nabc 12 main/binary-amd64/Packages'
        '\ndef 34 main/binary-amd64/Packages.gz'
    )


def test_stanzas_repeated_blank_lines():
    "Runs of blank lines separate stanzas without yielding empty ones"
    out = list(stanzas(['\n', 'A: 1\n', '\n', '\n', 'B: 2\n', '\n']))
    assert out == [{'A': '1'}, {'B': '2'}]


def test_stanzas_truncated():
    "A file cut mid-stanza yields what it has; the caller checks hashes"
    cut = PACKAGES[: PACKAGES.index('Version: 1:1.3')]
    *_, last = stanzas(io.StringIO(cut))
    assert last == {'Package': 'zlib1g', 'Architecture': 'amd64'}


def test_stanzas_continuation_before_field():
    "A leading continuation line is malformed input"
    with pytest.raises(KeyError):
        list(stanzas([' orphan\n']))


def test_stanzas_gzip_stream():
    "Compressed indexes stream through gzip.open in text mode"
    raw = gzip.compress(PACKAGES.encode())
    with gzip.open(io.BytesIO(raw), 'rt') as fh:
        names = [s['Package'] for s in stanzas(fh)]
    assert names == ['openssh-server', 'zlib1g']
