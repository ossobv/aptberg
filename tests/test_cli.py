import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from aptberg import cli, sync
from aptberg.fetch import fetch_suite
from aptberg.pool import Selection, SyncResult, select
from aptberg.snapshot import record
from aptberg.store import StoreError
from aptberg.sync import Scope, Sync

from .helpers import (
    SEC,
    UP,
    World,
    group_world,
    move_noble,
    signer,
    stanza,
)

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
    record(w.cut(w.suite('noble')), tmp_path / 'manifests', 'bkt', stage='acc')
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


def test_promote_cur_to_acc(tmp_path, monkeypatch, capsys):
    "--stage=cur cuts acc from what cur serves, without listing _pool/"
    w, config = _setup(tmp_path, monkeypatch)
    (tmp_path / 'manifests' / 'ubuntu' / 'acc.yaml').unlink()
    record(w.cut(w.suite('noble')), tmp_path / 'manifests', 'bkt')
    assert cli.main(['-c', config, 'apply', 'ubuntu/cur']) == 0
    capsys.readouterr()

    w.store.listed.clear()
    argv = ['-c', config, 'promote', '--stage=cur', '--apply', 'ubuntu']
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert 'noble: (none) -> 20260922a (cur serves it)' in out
    assert 'ubuntu/ch/acc/: applied' in out
    assert not any(POOL_PREFIX in c for c in w.store.listed)
    acc = tmp_path / 'manifests' / 'ubuntu' / 'acc.yaml'
    assert 'noble: 20260922a' in acc.read_text()

    with pytest.raises(SystemExit):
        cli.main(['-c', config, 'promote', '--stage=prod', 'ubuntu'])


def test_cut_apply(tmp_path, monkeypatch, capsys):
    "cut --apply writes and applies cur right away, for cron; never acc"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)

    assert cli.main(['-c', str(config), 'cut', '--apply']) == 0
    out = capsys.readouterr().out
    assert 'ubuntu/ch/cur/: applied' in out
    assert 'ubuntu/ch/cur/dists/noble/InRelease' in w.store.objects
    assert not any(k.startswith('ubuntu/ch/acc') for k in w.store.objects)
    assert not (tmp_path / 'manifests' / 'ubuntu' / 'acc.yaml').exists()

    w.store.listed.clear()
    assert cli.main(['-c', str(config), 'cut', '--apply']) == 0
    out = capsys.readouterr().out
    assert out.count('up to date') == 1
    # Nothing changed, so nothing was cut: no pool listed.
    assert not any(POOL_PREFIX in c for c in w.store.listed)


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


def test_cut_stage_is_only_cur(tmp_path, monkeypatch, capsys):
    "--stage=cur still works, for old cron lines; acc points to promote"
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

    for stage in ('acc', 'bogus'):
        with pytest.raises(SystemExit):
            cli.main(['-c', str(config), 'cut', '--stage', stage])
        assert 'promote --stage=cur' in capsys.readouterr().err


def test_cut_without_apply_does_not_serve(tmp_path, monkeypatch, capsys):
    "plain cut writes the cur manifest but leaves the prefix unserved"
    w = World(tmp_path)
    w.sync_pool()
    config = tmp_path / 'aptberg.yaml'
    config.write_text(CONFIG.format(keyring=signer().keyring))
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)

    assert cli.main(['-c', str(config), 'cut']) == 0
    manifest = tmp_path / 'manifests' / 'ubuntu' / 'cur.yaml'
    assert 'noble: 20' in manifest.read_text()
    assert not any(k.startswith('ubuntu/ch/') for k in w.store.objects)
    assert not (tmp_path / 'manifests' / 'ubuntu' / 'acc.yaml').exists()


def test_apply_does_not_list_the_pool(tmp_path, monkeypatch):
    "_pool/ is whole-bucket-sized, and apply has no need to list it"
    w, config = _setup(tmp_path, monkeypatch)
    calls = []
    real_list_sizes = type(w.store).list_sizes

    def counting(self, prefix):
        calls.append(prefix)
        return real_list_sizes(self, prefix)

    monkeypatch.setattr(type(w.store), 'list_sizes', counting)
    assert cli.main(['-c', config, 'apply', 'ubuntu/acc', 'ubuntu/acc']) == 0
    assert calls.count(POOL_PREFIX) == 0


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
    args = ['-c', config, 'diff', '20260922a..20260922b', '--suite=noble']
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert 'ubuntu noble: 20260922a -> 20260922b' in out
    assert '  main/binary-amd64:' in out
    assert 'app 1.0 -> 2.0' in out
    # unwanted is in the indexes, not the pool: no host can install it.
    assert 'unwanted' not in out
    assert cli.main([*args, '--unmirrored']) == 0
    assert 'unwanted 1.0 -> (none)' in capsys.readouterr().out

    args = ['-c', config, 'diff', '20260922a..20260922a', '--suite=noble']
    assert cli.main(args) == 0
    assert 'no package differences' in capsys.readouterr().out


def _served_acc_cur(w, config):
    "acc serves 20260922a, cur 20260922b"
    move_noble(w, stage='cur')
    for prefix in ('ubuntu/acc', 'ubuntu/cur'):
        assert cli.main(['-c', config, 'apply', prefix]) == 0


def test_diff_stages(tmp_path, monkeypatch, capsys, caplog):
    "diff acc..cur: each suite's snapshot as its prefix serves it"
    w, config = _setup(tmp_path, monkeypatch)
    assert cli.main(['-c', config, 'diff', 'acc..cur']) == 1
    assert 'serves nothing' in caplog.text
    assert 'no suite to compare' in caplog.text
    _served_acc_cur(w, config)
    capsys.readouterr()
    assert cli.main(['-c', config, 'diff', 'acc..cur', 'ubuntu']) == 0
    out = capsys.readouterr().out
    assert 'ubuntu noble: 20260922a -> 20260922b' in out
    assert 'app 1.0 -> 2.0' in out
    assert cli.main(['-c', config, 'diff', 'acc..acc']) == 0
    assert 'no package differences in acc..acc' in capsys.readouterr().out
    assert cli.main(['-c', config, 'diff', 'acc', 'cur']) == 1
    assert "not OLD..NEW: 'acc'" in caplog.text
    assert cli.main(['-c', config, 'diff', 'cur..acc']) == 0
    assert 'for what is coming, try acc..cur' in caplog.text


def test_diff_files(tmp_path, monkeypatch, capsys):
    "diff --files: one line per new pool file, to fetch and check"
    w, config = _setup(tmp_path, monkeypatch)
    _served_acc_cur(w, config)
    capsys.readouterr()
    args = ['-c', config, 'diff', 'acc..cur', '--files']
    assert cli.main(args) == 0
    lines = capsys.readouterr().out.splitlines()
    content = b'app 2.0 amd64'
    assert lines == [
        '# codename suites component size sha256 path',
        f'noble noble main {len(content)} {hashlib.sha256(content).hexdigest()} '
        'ubuntu/ch/cur/pool/main/a/app/app_2.0_amd64.deb',
    ]
    assert cli.main([*args, '--source=nomatch']) == 0
    assert 'no new pool files' in capsys.readouterr().out
    assert cli.main([*args, '--json']) == 0
    (got,) = json.loads(capsys.readouterr().out)
    assert got['package'] == 'app' and got['version'] == '2.0'
    assert got['codename'] == 'noble' and got['suites'] == ['noble']


GROUP_CONFIG = (
    'scratch: scratch\nbucket: bkt\nmanifests: manifests\n'
    'upstreams:\n'
    '  ubuntu:\n    group: ubuntu\n    url: http://up.example/ubuntu\n'
    '    keyring: {keyring}\n    pool: _shared\n'
    '    suites:\n      noble: [main]\n      noble-updates: [main]\n'
    '    architectures: [amd64]\n'
    '  ubuntu-security:\n    group: ubuntu\n'
    '    url: http://up.example/ubuntu\n'
    '    keyring: {keyring}\n    pool: _shared\n'
    '    suites:\n      noble-security: [main]\n'
    '    architectures: [amd64]\n'
)


def _cut_cur(w, config, up, suite, packages):
    "Republish suite with packages, cut it as cur and apply that"
    w.archive.add_suite(
        '/ubuntu', suite, {'main/binary-amd64': packages}, codename='noble'
    )
    fs = fetch_suite(
        w.archive.client(), up, up.sources()[0], suite, w.tmp_path / 'scratch'
    )
    w.suites = [fs if s.suite == suite else s for s in w.suites]
    w.selection = select(w.suites)
    w.sync_pool()
    record(w.cut(fs), w.tmp_path / 'manifests', 'bkt', stage='cur')
    assert cli.main(['-c', config, 'apply', f'{up.name}/cur']) == 0


def test_diff_group_files(tmp_path, monkeypatch, capsys):
    "diff names a group: its -updates and -security, a shared file once"
    w = group_world(tmp_path)
    config = tmp_path / 'aptberg.yaml'
    config.write_text(GROUP_CONFIG.format(keyring=signer().keyring))
    config = str(config)
    monkeypatch.setattr(cli, '_store', lambda cfg: w.store)
    fix = stanza('app', '1.0.3', Depends='libfoo')
    _cut_cur(w, config, UP, 'noble-updates', [fix])
    _cut_cur(w, config, SEC, 'noble-security', [fix])
    _cut_cur(w, config, UP, 'noble', [stanza('app'), stanza('extra')])
    capsys.readouterr()
    args = ['-c', config, 'diff', 'acc..cur']
    assert cli.main([*args, 'ubuntu', '--files']) == 0
    lines = capsys.readouterr().out.splitlines()
    fixed = b'app 1.0.3 amd64'
    extra = b'extra 1.0 amd64'
    fixed_line = (
        f'noble noble-security,noble-updates main {len(fixed)} '
        f'{hashlib.sha256(fixed).hexdigest()} '
        'ubuntu/ch/cur/pool/main/a/app/app_1.0.3_amd64.deb'
    )
    # extra is named by noble's index only: the filter takes app.
    assert lines == [
        '# codename suites component size sha256 path',
        fixed_line,
    ]
    assert cli.main([*args, 'ubuntu', '--files', '--unmirrored']) == 0
    assert capsys.readouterr().out.splitlines() == [
        '# codename suites component size sha256 path',
        fixed_line,
        f'noble noble main {len(extra)} {hashlib.sha256(extra).hexdigest()} '
        'ubuntu/ch/cur/pool/main/e/extra/extra_1.0_amd64.deb',
    ]
    # A member that is not the group's namesake means only itself.
    assert cli.main([*args, 'ubuntu-security', '--files']) == 0
    (_, line) = capsys.readouterr().out.splitlines()
    assert line.startswith('noble noble-security main ')
    assert line.endswith(
        ' ubuntu-security/ch/cur/pool/main/a/app/app_1.0.3_amd64.deb'
    )


def test_diff_json(tmp_path, monkeypatch, capsys):
    "diff --json: added, removed and changed per index, for programs"
    w, config = _setup(tmp_path, monkeypatch)
    move_noble(w)
    args = ['-c', config, 'diff', '20260922a..20260922b', '--suite=noble']
    assert cli.main([*args, '--json']) == 0
    (got,) = json.loads(capsys.readouterr().out)
    assert got['upstream'] == 'ubuntu' and got['suite'] == 'noble'
    assert got['a'] == '20260922a' and got['b'] == '20260922b'
    head = got['heads']['main/binary-amd64']
    assert head['changed']['app'] == ['1.0', '2.0']
    assert 'unwanted' not in head['removed']
    assert cli.main([*args, '--json', '--unmirrored']) == 0
    (got,) = json.loads(capsys.readouterr().out)
    assert 'unwanted' in got['heads']['main/binary-amd64']['removed']


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
            ['-c', str(config), 'apply', 'ubuntu/cur', 'kubernetes/stable-cur']
        )
        == 0
    )
    manifests = tmp_path / 'manifests'

    # verystable and experimental cur were never applied: refused, alone
    argv = ['-c', str(config), 'promote', '--stage=cur', '--apply']
    assert cli.main(argv) == 1
    assert 'kubernetes verystable: ' in caplog.text
    assert (manifests / 'kubernetes' / 'stable-acc.yaml').exists()
    assert not (manifests / 'kubernetes' / 'verystable-acc.yaml').exists()
    capsys.readouterr()
    caplog.clear()

    # acc -> prod: verystable and experimental have no acc and no prod
    # yet: new, so skipped with a warning, not refused
    assert cli.main(['-c', str(config), 'promote']) == 0
    assert 'kubernetes verystable: new, not promoted to acc' in caplog.text
    assert 'kubernetes experimental: new, not promoted to acc' in caplog.text
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

    # and no other upstream's Release is fetched to find that out: a
    # sync of it works while their servers are down
    probed = []
    probe = sync.probe_family
    monkeypatch.setattr(
        sync,
        'probe_family',
        lambda http, up, *a: probed.append(up.name) or probe(http, up, *a),
    )
    assert names(['kubernetes']) == {'kubernetes'}
    assert probed == []
    assert names(['debian']) == {'debian', 'kubernetes'}
    assert set(probed) == {'ubuntu', 'debian', 'kubernetes'}
