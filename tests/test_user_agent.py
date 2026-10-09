import subprocess
from datetime import datetime, timezone

from botocore.awsrequest import AWSResponse

from aptberg import __version__, git_version, user_agent
from aptberg.config import load
from aptberg.fetch import client
from aptberg.store import Store


def test_user_agent():
    "aptberg/<version>, with an optional contact that cannot break out"
    assert user_agent() == f'aptberg/{__version__}'
    assert user_agent('ops@example.com') == (
        f'aptberg/{__version__} (ops@example.com)'
    )
    assert user_agent('a (b)\nc') == f'aptberg/{__version__} (a b c)'


def test_http_client():
    "Every upstream request carries it"
    assert client().headers['user-agent'] == user_agent()
    assert client(agent='aptberg/x (y)').headers['user-agent'] == (
        'aptberg/x (y)'
    )


def test_s3_requests(monkeypatch):
    "boto3's own User-Agent, with ours appended"
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    store = Store(
        'bkt',
        endpoint='https://s3.invalid',
        region='us-east-1',
        agent='aptberg/9.9 (ops@example.com)',
    )
    seen = []

    class Raw:
        def stream(self, **kwargs):
            return iter([b''])

    def send(request, **kwargs):
        seen.append(request.headers['User-Agent'].decode())
        return AWSResponse(request.url, 404, {}, Raw())

    store.s3.meta.events.register('before-send.s3', send)
    assert store.sha256('k') is None
    assert seen and 'Boto3/' in seen[0]
    assert 'aptberg/9.9 (ops@example.com)' in seen[0]


def test_config_contact(tmp_path):
    "contact: in the config ends up in the agent"
    (tmp_path / 'aptberg.yaml').write_text(
        'scratch: s\nbucket: b\ncontact: ops@example.com\nupstreams:\n'
        '  u:\n    url: http://x\n    keyring: k\n'
        '    suites:\n      s: [main]\n    architectures: [amd64]\n'
    )
    cfg = load(tmp_path / 'aptberg.yaml')
    assert cfg.user_agent == f'aptberg/{__version__} (ops@example.com)'


def _git(root, *args):
    subprocess.run(
        [
            'git',
            '-C',
            str(root),
            '-c',
            'user.name=t',
            '-c',
            'user.email=t@example.com',
            *args,
        ],
        check=True,
        capture_output=True,
    )


def test_git_version(tmp_path):
    "Tag, dirty and later tips are reported as setuptools-scm has them"
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "aptberg"\n')
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'add', '.')
    _git(tmp_path, 'commit', '-q', '-m', 'one')
    assert git_version(tmp_path) is None  # no tag yet
    _git(tmp_path, 'tag', 'v0.1')
    assert git_version(tmp_path) == '0.1'
    node = subprocess.run(
        ['git', '-C', str(tmp_path), 'rev-parse', '--short', 'HEAD'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / 'pyproject.toml').write_text(
        '[project]\nname = "aptberg"\n# edit\n'
    )
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    assert git_version(tmp_path) == f'0.2.dev0+g{node}.d{today}'
    _git(tmp_path, 'commit', '-q', '-am', 'two')
    clean = git_version(tmp_path)
    assert clean.startswith('0.2.dev1+g')
    assert '.' not in clean.partition('+')[2]  # no date: not dirty


def test_git_version_elsewhere(tmp_path):
    "Not a checkout of aptberg: no git version"
    assert git_version(tmp_path) is None
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "other"\n')
    _git(tmp_path, 'init', '-q')
    assert git_version(tmp_path) is None
