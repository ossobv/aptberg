"""Streaming deb822 stanza parser

Used for Release and InRelease payloads, and Packages and Sources
indexes. Input is any iterable of lines, so callers can hand in a
decompressing text stream (index.open_text); a Packages file is tens of
MB and is never read whole.
"""

from collections.abc import Iterable, Iterator


def stanzas(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """Yield each stanza as a field -> value dict

    Continuation lines are joined to their field with a newline, their
    single leading space or tab removed. A continuation line before any
    field raises KeyError: that input is malformed.
    """
    fields: dict[str, str] = {}
    key = ''
    for line in lines:
        line = line.rstrip('\n')
        if not line:
            if fields:
                yield fields
                fields, key = {}, ''
        elif line[0] in ' \t':
            fields[key] += '\n' + line[1:]
        else:
            key, _, value = line.partition(':')
            fields[key] = value.lstrip()
    if fields:
        yield fields
