"""aptberg: apt mirror with moving snapshots, stored in S3"""

import re
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

_DESCRIBE = re.compile(
    r'v?(?P<tag>\d+(?:\.\d+)*)-(?P<distance>\d+)-'
    r'g(?P<node>[0-9a-f]+)(?P<dirty>-dirty)?'
)


def git_version(root: Path) -> str | None:
    """The version of a git checkout of aptberg, as setuptools-scm has it

    v0.1 exactly and clean is 0.1. Anything else is the next version's
    dev release: 0.2.dev3+g1a2b3c4, with .d20260922 (UTC) when the tree
    is dirty. None when root is not a tagged git checkout of aptberg.
    """
    try:
        if 'name = "aptberg"' not in (root / 'pyproject.toml').read_text():
            return None
        proc = subprocess.run(
            [
                'git',
                '-C',
                str(root),
                'describe',
                '--tags',
                '--long',
                '--dirty',
                '--match',
                'v[0-9]*',
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _DESCRIBE.fullmatch(proc.stdout.strip())
    if proc.returncode != 0 or match is None:
        return None
    tag, distance = match['tag'], int(match['distance'])
    if distance == 0 and not match['dirty']:
        return tag
    parts = tag.split('.')
    parts[-1] = str(int(parts[-1]) + 1)
    local = f'g{match["node"]}'
    if match['dirty']:
        local += datetime.now(timezone.utc).strftime('.d%Y%m%d')
    return f'{".".join(parts)}.dev{distance}+{local}'


def _version() -> str:
    # An editable install keeps the version it had when installed; in a
    # checkout, ask git instead, so every tip reports itself.
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / '.git').exists():
        found = git_version(checkout)
        if found:
            return found
    try:
        return version('aptberg')
    except PackageNotFoundError:  # run from a source tree, not installed
        return 'unknown'


__version__ = _version()


def user_agent(contact: str | None = None) -> str:
    """What aptberg calls itself, to upstream mirrors and to the bucket

    aptberg/<version>, with an optional contact in parentheses so that
    whoever runs an upstream mirror knows whom to ask about the traffic.
    """
    agent = f'aptberg/{__version__}'
    if contact:
        clean = ' '.join(contact.replace('(', '').replace(')', '').split())
        agent += f' ({clean})'
    return agent
