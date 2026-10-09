import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from aptberg import cli, sync
from aptberg.pool import Selection, SyncResult
from aptberg.snapshot import record
from aptberg.store import StoreError
from aptberg.sync import Scope, Sync

from .helpers import World, group_world, move_noble, signer, stanza

POOL_PREFIX = '_pool/_shared/'

CONFIG = """\
scratch: scratch
bucket: bkt
manifests: manifests
upstreams:
  ubuntu:
    url: http://up.example/ubuntu
    keyring: {keyring}
    pool: _shared
    suites:
      noble: [main]
    architectures: [amd64]
    filter:
      include: [app]
"""


def _setup(tmp_path, monkeypatch):
    "A cut noble recorded in ubuntu/acc.yaml, and a config pointing at it"
    w = World(tmp_path)
    w.sync_pool()
    record(w.cut(w.suite('noble')), tmp_path / 'manifests', 'bkt')
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    return w, str(config)


def test_apply_and_promote(tmp_path, monkeypatch, capsys, caplog):
    "apply acc, then promote to prod, through the command line"
    w, config = _setup(tmp_path, monkeypatch)
    assert cli.main(['-c', config, 'promote', 'ubuntu']) == 1
    assert 'apply acc first' in caplog.text

    assert cli.main(['-c', config, 'apply', 'ubuntu/acc']) == 0
    assert 'applied, 11 ops' in capsys.readouterr().out
    assert cli.main(['-c', config, 'apply', 'ubuntu/acc']) == 0
    assert 'up to date' in capsys.readouterr().out

    prod = tmp_path / 'manifests' / 'ubuntu' / 'prod.yaml'
    assert cli.main(['-c', config, 'promote', 'ubuntu', '--dry-run']) == 0
    out = capsys.readouterr().out
    assert 'noble: (none) -> 20260922a' in out
    assert 'release  3 copies' in out
    assert not prod.exists()

    assert cli.main(['-c', config, 'promote', 'ubuntu', '--apply']) == 0
    assert 'applied, 11 ops' in capsys.readouterr().out
    assert 'noble: 20260922a' in prod.read_text()
    assert (
        w.store.objects['ubuntu/ch/prod/dists/noble/InRelease']
        == (w.store.objects['ubuntu/ch/acc/dists/noble/InRelease'])
    )


def test_cut_apply(tmp_path, monkeypatch, capsys):
    "cut --apply writes and applies cur and acc right away, for cron"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)

    assert cli.main(['-c', str(config), 'cut', '--apply']) == 0
    out = capsys.readouterr().out
    assert 'ubuntu/ch/acc/: applied' in out
    assert 'ubuntu/ch/cur/: applied' in out
    assert 'ubuntu/ch/acc/dists/noble/InRelease' in w.store.objects
    assert 'ubuntu/ch/cur/dists/noble/InRelease' in w.store.objects
    assert (
        w.store.objects['ubuntu/ch/cur/dists/noble/InRelease']
        == (w.store.objects['ubuntu/ch/acc/dists/noble/InRelease'])
    )

    assert cli.main(['-c', str(config), 'cut', '--apply']) == 0
    out = capsys.readouterr().out
    assert out.count('up to date') == 2


def test_cut_apply_lists_pool_once(tmp_path, monkeypatch, capsys):
    "cut --apply shares one _pool/ listing with the apply it triggers"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)

    w.store.listed.clear()
    assert cli.main(['-c', str(config), 'cut', '--apply']) == 0
    capsys.readouterr()
    pool_lists = [c for c in w.store.listed if c.endswith(POOL_PREFIX)]
    assert pool_lists == [f'sizes:{POOL_PREFIX}']


def test_sync_apply_lists_pool_once(tmp_path, monkeypatch, capsys):
    "sync --apply shares one _pool/ listing across upload, cut and apply"
    w = World(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    monkeypatch.setattr(sync, 'client', lambda *a: w.archive.client())

    assert cli.main(['-c', str(config), 'sync', '--apply']) == 0
    out = capsys.readouterr().out
    assert 'ubuntu/ch/acc/: applied' in out
    assert 'ubuntu/ch/cur/: applied' in out
    pool_lists = [c for c in w.store.listed if c.endswith(POOL_PREFIX)]
    assert pool_lists == [f'etags:{POOL_PREFIX}']


def test_sync_dry_run_does_not_pretend_pool_is_uploaded(
    tmp_path, monkeypatch, caplog
):
    "sync --dry-run --apply uploads nothing, so cut still sees it missing"
    w = World(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    monkeypatch.setattr(sync, 'client', lambda *a: w.archive.client())

    assert cli.main(['-c', str(config), 'sync', '--dry-run', '--apply']) == 1
    assert 'not in _pool/' in caplog.text
    assert w.store.objects == {}


def test_cut_stage_cur_leaves_acc_untouched(tmp_path, monkeypatch, capsys):
    "--stage=cur writes and applies only cur; acc is never touched"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)

    assert (
        cli.main(['-c', str(config), 'cut', '--stage', 'cur', '--apply']) == 0
    )
    out = capsys.readouterr().out
    assert 'ubuntu/ch/cur/: applied' in out
    assert 'ubuntu/ch/acc' not in out
    assert 'ubuntu/ch/cur/dists/noble/InRelease' in w.store.objects
    assert not any(k.startswith('ubuntu/ch/acc') for k in w.store.objects)
    assert not (tmp_path / 'manifests' / 'ubuntu' / 'acc.yaml').exists()

    with pytest.raises(SystemExit):
        cli.main(['-c', str(config), 'cut', '--stage', 'bogus'])


def test_cut_without_apply_does_not_serve(tmp_path, monkeypatch, capsys):
    "plain cut writes cur and acc manifests but leaves prefixes unserved"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)

    assert cli.main(['-c', str(config), 'cut']) == 0
    for stage in ('cur', 'acc'):
        manifest = tmp_path / 'manifests' / 'ubuntu' / f'{stage}.yaml'
        assert 'noble: 20' in manifest.read_text()
        assert not any(
            k.startswith(f'ubuntu/ch/{stage}') for k in w.store.objects
        )


def test_apply_lists_the_pool_once_for_several_manifests(
    tmp_path, monkeypatch
):
    "_pool/ is shared and whole-bucket-sized; N manifests list it once"
    w, config = _setup(tmp_path, monkeypatch)
    calls = []
    real_list_sizes = type(w.store).list_sizes

    def counting(self, prefix):
        calls.append(prefix)
        return real_list_sizes(self, prefix)

    monkeypatch.setattr(type(w.store), 'list_sizes', counting)
    assert cli.main(['-c', config, 'apply', 'ubuntu/acc', 'ubuntu/acc']) == 0
    assert calls.count(POOL_PREFIX) == 1


@pytest.fixture
def log_level():
    "Restore the aptberg log level a command set"
    level = cli.log.level
    yield
    cli.log.setLevel(level)


def test_quiet(tmp_path, monkeypatch, capsys, caplog, log_level):
    "--quiet prints nothing and logs no INFO; the exit code still tells"
    w, config = _setup(tmp_path, monkeypatch)
    caplog.clear()
    assert cli.main(['-c', config, '-q', 'apply', 'ubuntu/acc']) == 0
    assert capsys.readouterr().out == ''
    assert 'INFO' not in caplog.text
    assert 'ubuntu/ch/acc/dists/noble/InRelease' in w.store.objects

    assert (
        cli.main(
            ['-c', config, '--quiet', 'promote', 'ubuntu', '--suite', 'jammy']
        )
        == 1
    )
    assert capsys.readouterr().out == ''
    assert 'ERROR' in caplog.text


def test_json_logs_no_info(tmp_path, monkeypatch, capsys, caplog, log_level):
    "--json output is for programs: no INFO lines alongside it"
    w, config = _setup(tmp_path, monkeypatch)
    caplog.clear()
    assert cli.main(['-c', config, 'status', '--json']) == 0
    assert capsys.readouterr().out.startswith('[')
    assert 'INFO' not in caplog.text
    assert cli.log.level == logging.WARNING


def test_diff_via_cli(tmp_path, monkeypatch, capsys):
    "diff reports what changed between two cut snapshots"
    w, config = _setup(tmp_path, monkeypatch)
    move_noble(w)
    assert (
        cli.main(
            ['-c', config, 'diff', 'ubuntu', 'noble', '20260922a', '20260922b']
        )
        == 0
    )
    out = capsys.readouterr().out
    assert 'main/binary-amd64:' in out
    assert 'app 1.0 -> 2.0' in out
    assert 'unwanted 1.0 -> (none)' in out

    assert (
        cli.main(
            ['-c', config, 'diff', 'ubuntu', 'noble', '20260922a', '20260922a']
        )
        == 0
    )
    assert 'no package differences' in capsys.readouterr().out


def test_diff_json(tmp_path, monkeypatch, capsys):
    "diff --json: added, removed and changed per index, for programs"
    w, config = _setup(tmp_path, monkeypatch)
    move_noble(w)
    args = ['-c', config, 'diff', 'ubuntu', 'noble', '20260922a', '20260922b']
    assert cli.main([*args, '--json']) == 0
    got = json.loads(capsys.readouterr().out)['main/binary-amd64']
    assert got['changed']['app'] == ['1.0', '2.0']
    assert 'unwanted' in got['removed']


def _fail_store(cfg):
    "Stands in for a real _store() whose Store.check() failed"
    raise StoreError('bad credentials')


def _fail(*a, **kw):
    pytest.fail('did expensive local work before checking the store')


def test_fetch_checks_store_before_fetching(tmp_path, monkeypatch):
    "A store that fails its check aborts before any upstream fetch"
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', _fail_store)
    monkeypatch.setattr(Sync, 'load', _fail)
    assert cli.main(['-c', str(config), 'fetch']) == 1


def test_fetch_no_pool_skips_the_check(tmp_path, monkeypatch):
    "--no-pool never touches the bucket, so a bad store does not block it"
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', _fail_store)
    monkeypatch.setattr(Sync, 'load', lambda self, offline: self.use([]))
    assert cli.main(['-c', str(config), 'fetch', '--no-pool']) == 0


class _Selection(Selection):
    "A selection that restricts to itself: its suites have no indexes"

    def restrict(self, upstreams, suites):
        return self


def _listed_by(monkeypatch, tmp_path, command):
    "The _pool/ prefixes listed running command with 'ubuntu' named"
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    listed = []

    class Store:
        def list_etags(self, prefix):
            listed.append(prefix)
            return {}

    def up(name):
        return SimpleNamespace(name=name, pool_name=name)

    suites = [
        SimpleNamespace(upstream=up('ubuntu')),
        SimpleNamespace(upstream=up('other')),
    ]
    monkeypatch.setattr(cli, '_store', lambda cfg: Store())
    monkeypatch.setattr(Sync, 'load', lambda self, offline: self.use(suites))
    monkeypatch.setattr(cli.pool, 'select', lambda suites: _Selection())
    monkeypatch.setattr(cli.pool, 'sync', lambda *a, **kw: SyncResult())
    monkeypatch.setattr(cli, '_cut', lambda *a: 0)
    assert cli.main(['-c', str(config), command, 'ubuntu']) == 0
    return listed


def test_fetch_lists_only_the_named_upstreams_pool(tmp_path, monkeypatch):
    "fetch UPSTREAM lists its own _pool/ prefix, not the whole pool"
    assert _listed_by(monkeypatch, tmp_path, 'fetch') == ['_pool/ubuntu/']


def test_sync_lists_only_the_named_upstreams_pool(tmp_path, monkeypatch):
    "sync UPSTREAM does not list the pools of suites loaded for closure"
    assert _listed_by(monkeypatch, tmp_path, 'sync') == ['_pool/ubuntu/']


def test_sync_checks_store_before_fetching(tmp_path, monkeypatch):
    "sync checks the store before fetching too"
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', _fail_store)
    monkeypatch.setattr(Sync, 'load', _fail)
    assert cli.main(['-c', str(config), 'sync']) == 1


def test_cut_checks_store_before_reading_scratch(tmp_path, monkeypatch):
    "cut checks the store before loading or selecting from scratch"
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', _fail_store)
    monkeypatch.setattr(Sync, 'load', _fail)
    assert cli.main(['-c', str(config), 'cut']) == 1


def test_apply_checks_store_before_loading_manifests(tmp_path, monkeypatch):
    "apply checks the store before reading or diffing any manifest"
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', _fail_store)
    monkeypatch.setattr(cli, '_manifest', _fail)
    assert cli.main(['-c', str(config), 'apply', 'ubuntu/acc']) == 1


def test_keyboard_interrupt_is_a_clean_exit(monkeypatch, caplog):
    "^C during any command is a short message and exit 130, not a traceback"
    monkeypatch.setattr(
        cli, '_config', lambda args: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    assert cli.main(['status']) == 130
    assert 'interrupted' in caplog.text


GROUPED = """\
scratch: scratch
bucket: bkt
manifests: manifests
upstreams:
  ubuntu:
    url: http://up.example/ubuntu
    keyring: {keyring}
    group: g
    pool: _shared
    suites:
      noble: [main]
    architectures: [amd64]
    filter:
      include: [app]
  kubernetes:
    keyring: {keyring}
    group: g
    pool: _shared
    suites:
      anydist: [main]
    architectures: [amd64]
    channels:
      verystable: {{tree: "1.30", url: "http://up.example/k8s-1.30"}}
      stable: {{tree: "1.31", url: "http://up.example/k8s-1.31"}}
      experimental: {{tree: "1.31", url: "http://up.example/k8s-1.31"}}
"""


def test_cut_group(tmp_path, monkeypatch, capsys, caplog):
    "Naming one member cuts the group; mixed fetch runs are refused"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(GROUPED.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    stamp = (
        tmp_path
        / 'scratch'
        / 'kubernetes'
        / '1.31'
        / 'anydist'
        / ('.fetch-run')
    )
    stamp.write_text('another-run\n')
    assert cli.main(['-c', str(config), 'cut', 'ubuntu']) == 1
    assert 'g: scratch holds indexes of different fetch runs' in caplog.text
    assert not any(k.startswith('_snap/') for k in w.store.objects)

    stamp.write_text('\n')
    assert cli.main(['-c', str(config), 'cut', 'ubuntu']) == 0
    out = capsys.readouterr().out
    assert 'ubuntu noble: 20' in out
    assert 'kubernetes 1.30 anydist: 1.30/20' in out
    assert 'kubernetes 1.31 anydist: 1.31/20' in out


def test_promote_group(tmp_path, monkeypatch, capsys):
    "promote with a member's name promotes and applies the whole group"
    w = group_world(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
        f'upstreams:\n'
        f'  ubuntu:\n    group: ubuntu\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble: [main]\n      noble-updates: [main]\n'
        f'    architectures: [amd64]\n'
        f'  ubuntu-security:\n    group: ubuntu\n'
        f'    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble-security: [main]\n'
        f'    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    assert (
        cli.main(['-c', str(config), 'promote', 'ubuntu-security', '--apply'])
        == 0
    )
    out = capsys.readouterr().out
    assert 'ubuntu/ch/prod/: applied' in out
    assert 'ubuntu-security/ch/prod/: applied' in out
    for up, suite in (
        ('ubuntu', 'noble'),
        ('ubuntu', 'noble-updates'),
        ('ubuntu-security', 'noble-security'),
    ):
        key = f'dists/{suite}/InRelease'
        assert (
            w.store.objects[f'{up}/ch/prod/{key}']
            == (w.store.objects[f'{up}/ch/acc/{key}'])
        )


def test_promote_everything(tmp_path, monkeypatch, capsys, caplog):
    "promote without arguments: every upstream and channel, each alone"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        GROUPED.replace('    group: g\n', '').format(keyring=signer().keyring)
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    assert cli.main(['-c', str(config), 'cut']) == 0
    assert (
        cli.main(
            ['-c', str(config), 'apply', 'ubuntu/acc', 'kubernetes/stable-acc']
        )
        == 0
    )
    capsys.readouterr()
    manifests = tmp_path / 'manifests'

    # verystable and experimental were never applied: refused, alone
    assert cli.main(['-c', str(config), 'promote']) == 1
    assert 'kubernetes verystable: ' in caplog.text
    assert 'kubernetes experimental: ' in caplog.text
    assert 'kubernetes stable: ' not in caplog.text
    assert (manifests / 'ubuntu' / 'prod.yaml').exists()
    assert (manifests / 'kubernetes' / 'stable-prod.yaml').exists()
    assert not (manifests / 'kubernetes' / 'verystable-prod.yaml').exists()

    capsys.readouterr()
    caplog.clear()
    assert (
        cli.main(
            ['-c', str(config), 'promote', '--channel', 'stable', '--dry-run']
        )
        == 0
    )
    out = capsys.readouterr().out
    assert 'stable-prod.yaml: nothing to promote' in out
    assert 'ubuntu' not in out

    assert (
        cli.main(['-c', str(config), 'promote', '--channel', 'nightly']) == 1
    )
    assert 'no channel nightly' in caplog.text


def test_cut_codename(tmp_path, monkeypatch, capsys, caplog):
    "--codename selects by Release codename, adding to --suite"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(GROUPED.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    cut = ['-c', str(config), 'cut', 'ubuntu', '--dry-run']

    assert cli.main([*cut, '--codename', 'noble']) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split(':')[0] for line in out] == ['ubuntu noble']

    assert cli.main([*cut, '--codename', 'anydist', '--suite', 'noble']) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split(':')[0] for line in out] == [
        'ubuntu noble',
        'kubernetes 1.30 anydist',
        'kubernetes 1.31 anydist',
    ]

    assert cli.main([*cut, '--codename', 'jammy']) == 1
    assert 'no suite with codename jammy' in caplog.text


def test_promote_codename(tmp_path, monkeypatch, capsys):
    "promote --codename noble promotes the family across the group"
    w = group_world(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(
        f'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
        f'upstreams:\n'
        f'  ubuntu:\n    group: ubuntu\n    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble: [main]\n      noble-updates: [main]\n'
        f'    architectures: [amd64]\n'
        f'  ubuntu-security:\n    group: ubuntu\n'
        f'    url: http://up.example/ubuntu\n'
        f'    keyring: {signer().keyring}\n    pool: _shared\n'
        f'    suites:\n      noble-security: [main]\n'
        f'    architectures: [amd64]\n'
    )
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    assert (
        cli.main(
            [
                '-c',
                str(config),
                'promote',
                'ubuntu',
                '--codename',
                'noble',
                '--dry-run',
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    for suite in ('noble', 'noble-updates', 'noble-security'):
        assert f'  {suite}: (none) -> 20260922a' in out


def test_naming_an_upstream_skips_other_families(tmp_path, monkeypatch):
    "Only suites that can influence the named upstream's selection load"
    w, config = _setup(tmp_path, monkeypatch)
    cfg = Path(config)
    cfg.write_text(
        cfg.read_text()
        + """\
  debian:
    url: http://up.example/debian
    keyring: {keyring}
    suites:
      bookworm: [main]
    architectures: [amd64]
    filter:
      include: [app]
  kubernetes:
    keyring: {keyring}
    suites:
      anydist: [main]
    architectures: [amd64]
    channels:
      stable: {{tree: "1.31", url: "http://up.example/k8s-1.31"}}
""".format(keyring=signer().keyring)
    )
    w.archive.add_suite(
        '/debian', 'bookworm', {'main/binary-amd64': [stanza('app')]}
    )
    http = w.archive.client()
    cfg = cli.config.load(cfg)
    monkeypatch.setattr(sync, 'client', lambda *a: http)

    def names(named):
        job = Sync(cfg, None, Scope(tuple(named)))
        job.load(offline=False)
        return {fs.upstream.name for fs in job.suites}

    assert names([]) == {'ubuntu', 'debian', 'kubernetes'}
    # the unfiltered kubernetes seeds every family, so it comes along
    assert names(['ubuntu']) == {'ubuntu', 'kubernetes'}
    assert names(['debian']) == {'debian', 'kubernetes'}
    # but it depends on nothing itself
    assert names(['kubernetes']) == {'kubernetes'}
