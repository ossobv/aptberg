import re
from pathlib import Path

import pytest

from aptberg.config import ConfigError, load, read_patterns
from aptberg.pool import storage_key

EXAMPLE = Path(__file__).parent.parent / 'aptberg.example.yaml'

CONFIG = """\
scratch: work
bucket: aptberg.example.com
manifests: ../manifests
upstreams:
  ubuntu:
    url: http://apt.example.com/ubuntu/
    keyring: keys/ubuntu.gpg
    suites:
      noble: [main]
      noble-updates: [main]
    architectures: [amd64, arm64]
    filter: &ubuntu-filter
      include: include-packages
  ubuntu-security:
    url: http://apt.example.com/ubuntu-security
    keyring: keys/ubuntu.gpg
    suites:
      noble-security: [main]
    architectures: [amd64]
    filter: *ubuntu-filter
  kubernetes:
    keyring: keys/ppa.gpg
    suites:
      anydist: [main]
    architectures: [amd64]
    channels:
      verystable: {tree: "1.30", url: http://ppa.example.com/k8s-1.30}
      stable: {tree: "1.31", url: http://ppa.example.com/k8s-1.31}
      experimental: {tree: "1.31", url: http://ppa.example.com/k8s-1.31}
"""


def _write(tmp_path, text=CONFIG, include='python3\n# comment\n\nzsh\n'):
    (tmp_path / 'include-packages').write_text(include)
    path = tmp_path / 'aptberg.yaml'
    path.write_text(text)
    return path


def test_origin_pins(tmp_path):
    "origin_pins: a list of mappings of origin and label, as strings"
    text = CONFIG.replace(
        '  kubernetes:',
        '  zabbix:\n    url: http://repo.example.com/z\n'
        '    keyring: keys/z.gpg\n    suites: {noble: [main]}\n'
        '    architectures: [amd64]\n'
        '    origin_pins: [{origin: Zabbix, label: 7.0}, {origin: Z}]\n'
        '  kubernetes:',
    )
    cfg = load(_write(tmp_path, text))
    assert cfg.upstreams['zabbix'].origin_pins == [
        {'origin': 'Zabbix', 'label': '7.0'},
        {'origin': 'Z'},
    ]
    assert cfg.upstreams['ubuntu'].origin_pins == []
    with pytest.raises(
        ConfigError, match=r"origin_pins\[0\]: unknown keys \['suite'\]"
    ):
        load(_write(tmp_path, text.replace('label: 7.0', 'suite: noble')))
    with pytest.raises(ConfigError, match='origin_pins: expected a list'):
        load(
            _write(
                tmp_path,
                text.replace(
                    '[{origin: Zabbix, label: 7.0}, {origin: Z}]',
                    '{origin: Zabbix}',
                ),
            )
        )
    with pytest.raises(ConfigError, match=r'origin_pins\[1\]: expected a map'):
        load(_write(tmp_path, text.replace('{origin: Z}', 'Z')))


def test_load(tmp_path):
    "Paths resolve against the config dir; anchors share a filter"
    cfg = load(_write(tmp_path))
    assert cfg.scratch == tmp_path / 'work'
    assert cfg.manifests == tmp_path / '../manifests'
    ubuntu = cfg.upstreams['ubuntu']
    assert ubuntu.url == 'http://apt.example.com/ubuntu'
    assert ubuntu.keyring == tmp_path / 'keys/ubuntu.gpg'
    assert ubuntu.filter.include == ('python3', 'zsh')
    assert 'Recommends' in ubuntu.filter.follow
    assert cfg.upstreams['ubuntu-security'].filter == ubuntu.filter
    (source,) = ubuntu.sources()
    assert source.url == 'http://apt.example.com/ubuntu'


def test_deb_src_defaults_on(tmp_path):
    "deb_src mirrors on by default, off when the config says so"
    cfg = load(_write(tmp_path))
    assert cfg.upstreams['ubuntu'].deb_src is True
    off = CONFIG.replace(
        '    filter: &ubuntu-filter\n',
        '    deb_src: false\n    filter: &ubuntu-filter\n',
        1,
    )
    cfg = load(_write(tmp_path, off))
    assert cfg.upstreams['ubuntu'].deb_src is False
    assert cfg.upstreams['ubuntu-security'].deb_src is True


def test_enabled_no_drops_upstream(tmp_path):
    "enabled: no removes the upstream, even with the rest of it broken"
    cfg = load(
        _write(
            tmp_path,
            CONFIG.replace(
                '  kubernetes:\n', '  kubernetes:\n    enabled: no\n'
            ),
        )
    )
    assert 'kubernetes' not in cfg.upstreams
    assert set(cfg.upstreams) == {'ubuntu', 'ubuntu-security'}
    only_disabled = (
        CONFIG.split('  kubernetes:')[0] + '  kubernetes:\n    enabled: no\n'
    )
    broken = load(_write(tmp_path, only_disabled))
    assert 'kubernetes' not in broken.upstreams


def test_enabled_defaults_on(tmp_path):
    "Without enabled:, an upstream is mirrored as normal"
    cfg = load(_write(tmp_path))
    assert 'kubernetes' in cfg.upstreams


def test_served_defaults_to_name(tmp_path):
    "Without path:, an upstream is served at its own name"
    cfg = load(_write(tmp_path))
    ubuntu = cfg.upstreams['ubuntu']
    assert ubuntu.served == 'ubuntu'
    assert cfg.served_upstreams('ubuntu') == {'ubuntu': ubuntu}


PATH_CONFIG = CONFIG.replace(
    '  kubernetes:\n',
    """\
  kubernetes:
    path: ubuntu
""",
)


def test_path_overrides_served_location(tmp_path):
    "path: serves an upstream under another upstream's name"
    cfg = load(_write(tmp_path, PATH_CONFIG))
    k8s = cfg.upstreams['kubernetes']
    assert k8s.served == 'ubuntu'
    assert k8s.name == 'kubernetes'
    served = cfg.served_upstreams('ubuntu')
    assert set(served) == {'ubuntu', 'kubernetes'}
    assert cfg.served_upstreams('kubernetes') == {}


def test_path_refuses_a_shared_suite_name(tmp_path):
    "Two upstreams serving the same path may not both name a suite"
    clashing = PATH_CONFIG.replace(
        '      anydist: [main]\n', '      noble: [main]\n'
    )
    with pytest.raises(
        ConfigError, match='noble.*upstreams.ubuntu.*upstreams.kubernetes'
    ):
        load(_write(tmp_path, clashing))


def test_paths_relative_to_config_not_cwd(tmp_path, monkeypatch):
    "Every relative path resolves against the config file's directory"
    conf = tmp_path / 'etc'
    conf.mkdir()
    (conf / 'soft').write_text('linux-source-*\n')
    path = _write(
        conf,
        CONFIG.replace(
            'include: include-packages',
            'include: include-packages\n      soft_exclude: soft',
        ),
    )
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = load('../etc/aptberg.yaml')
    ubuntu = cfg.upstreams['ubuntu']
    assert ubuntu.filter.include == ('python3', 'zsh')
    assert ubuntu.filter.soft_exclude == ('linux-source-*',)
    assert cfg.scratch == conf / 'work'
    assert ubuntu.keyring == conf / 'keys/ubuntu.gpg'
    assert cfg.scratch.is_absolute() and ubuntu.keyring.is_absolute()
    assert path.parent == conf


GROUPED = (
    CONFIG.replace(
        '    filter: &ubuntu-filter\n',
        '    group: ubuntu\n    filter: &ubuntu-filter\n',
    )
    .replace(
        '    filter: *ubuntu-filter\n',
        '    group: ubuntu\n    filter: *ubuntu-filter\n',
    )
    .replace(
        '    keyring: keys/ppa.gpg\n',
        '    keyring: keys/ppa.gpg\n    group: k8s\n',
    )
)


def test_groups(tmp_path):
    "A member or the group name means the whole group, in config order"
    cfg = load(_write(tmp_path, GROUPED))
    assert cfg.groups() == {
        'ubuntu': ['ubuntu', 'ubuntu-security'],
        'k8s': ['kubernetes'],
    }
    assert cfg.expand(['ubuntu-security']) == ['ubuntu', 'ubuntu-security']
    assert cfg.expand(['ubuntu']) == ['ubuntu', 'ubuntu-security']
    assert cfg.expand(['k8s']) == ['kubernetes']
    assert cfg.expand(['k8s', 'ubuntu']) == [
        'ubuntu',
        'ubuntu-security',
        'kubernetes',
    ]
    assert cfg.upstreams['kubernetes'].unit == 'k8s'
    with pytest.raises(ConfigError, match='unknown upstream or group'):
        cfg.expand(['debian'])


def test_group_name_clash(tmp_path):
    "A group may not be named after an upstream outside it"
    with pytest.raises(ConfigError, match='outside it'):
        load(
            _write(
                tmp_path,
                GROUPED.replace('group: k8s', 'group: ubuntu-security'),
            )
        )
    with pytest.raises(ConfigError, match='group'):
        load(_write(tmp_path, GROUPED.replace('group: k8s', 'group: _x')))


def test_ungrouped(tmp_path):
    "Without groups every upstream stands alone"
    cfg = load(_write(tmp_path))
    assert cfg.groups() == {}
    assert cfg.expand(['ubuntu']) == ['ubuntu']
    assert cfg.upstreams['ubuntu'].unit == 'ubuntu'


def test_tenant_bucket(tmp_path):
    "A tenant:bucket name survives YAML unquoted"
    cfg = load(
        _write(
            tmp_path,
            CONFIG.replace(
                'bucket: aptberg.example.com', 'bucket: tenant:aptberg'
            ),
        )
    )
    assert cfg.bucket == 'tenant:aptberg'


def test_channels_share_trees(tmp_path):
    "Two channels on one tree yield one source"
    k8s = load(_write(tmp_path)).upstreams['kubernetes']
    assert k8s.filter is None
    assert [(s.tree, s.url) for s in k8s.sources()] == [
        ('1.30', 'http://ppa.example.com/k8s-1.30'),
        ('1.31', 'http://ppa.example.com/k8s-1.31'),
    ]
    assert str(k8s.sources()[0].scratch_name) == 'kubernetes/1.30'


def test_suites_own_components(tmp_path):
    "Each suite names its own components; suites may differ"
    cfg = load(
        _write(
            tmp_path,
            CONFIG.replace(
                '    suites:\n      anydist: [main]\n',
                '    suites:\n      anydist: [main]\n      other: [extra]\n',
            ),
        )
    )
    k8s = cfg.upstreams['kubernetes']
    assert k8s.suites == ('anydist', 'other')
    assert k8s.components == {'anydist': ('main',), 'other': ('extra',)}


def test_suites_needs_a_mapping(tmp_path):
    "suites: a flat list of names is refused"
    with pytest.raises(ConfigError, match='expected a mapping'):
        load(
            _write(
                tmp_path,
                CONFIG.replace(
                    '    suites:\n      anydist: [main]\n',
                    '    suites: [anydist]\n',
                ),
            )
        )


def test_suites_empty(tmp_path):
    "An upstream needs at least one suite"
    with pytest.raises(ConfigError, match='suites: empty'):
        load(
            _write(
                tmp_path,
                CONFIG.replace(
                    '    suites:\n      anydist: [main]\n', '    suites: {}\n'
                ),
            )
        )


def test_suite_needs_components(tmp_path):
    "A suite with no components is an error, not a silent empty mirror"
    with pytest.raises(ConfigError, match='suites.anydist: empty'):
        load(
            _write(
                tmp_path,
                CONFIG.replace('      anydist: [main]', '      anydist: []'),
            )
        )


def test_unknown_key(tmp_path):
    "A typo is an error, not silently ignored"
    with pytest.raises(ConfigError, match='architecture'):
        load(
            _write(
                tmp_path,
                CONFIG.replace(
                    'architectures: [amd64, arm64]', 'architecture: [amd64]'
                ),
            )
        )


def test_excludes(tmp_path):
    "Pattern lists come from files or inline lists"
    (tmp_path / 'soft').write_text(
        '# kernels\nlinux-*[0-9].[0-9]*.[0-9]*-[0-9]*\n\nlinux-source-*\n'
    )
    cfg = load(
        _write(
            tmp_path,
            CONFIG.replace(
                'include: include-packages',
                'include: ["*"]\n      soft_exclude: soft\n'
                '      hard_exclude: ["*nvidia*"]',
            ),
        )
    )
    filt = cfg.upstreams['ubuntu'].filter
    assert filt.include == ('*',)
    assert filt.soft_exclude == (
        'linux-*[0-9].[0-9]*.[0-9]*-[0-9]*',
        'linux-source-*',
    )
    assert filt.hard_exclude == ('*nvidia*',)


def test_upstream_names(tmp_path):
    "Names starting with _ are aptberg's own key prefixes"
    for bad in ('_pool', 'Ubuntu', 'a/b'):
        with pytest.raises(ConfigError, match='upstream name'):
            load(
                _write(
                    tmp_path, CONFIG.replace('  ubuntu-security:', f'  {bad}:')
                )
            )


def test_filter_needs_include(tmp_path):
    "A filter without includes is an error, not a silent empty mirror"
    with pytest.raises(ConfigError, match='include'):
        load(
            _write(
                tmp_path,
                CONFIG.replace(
                    'include: include-packages', 'hard_exclude: [x]'
                ),
            )
        )
    with pytest.raises(ConfigError, match='empty'):
        load(
            _write(
                tmp_path,
                CONFIG.replace('include: include-packages', 'include: []'),
            )
        )


def test_patterns(tmp_path):
    "Names and globs are fine; relation syntax is refused"
    path = tmp_path / 'inc'
    path.write_text('python3\nlinux-image-*  # comment\npython3\n')
    assert read_patterns(path) == ('python3', 'linux-image-*')
    path.write_text('python3\nfoo (>= 1)\n')
    with pytest.raises(ConfigError, match=':2:'):
        read_patterns(path)


def test_pool(tmp_path):
    "pool names the pool; without it the unit is the pool"
    cfg = load(
        _write(
            tmp_path,
            CONFIG.replace(
                '    suites:\n', '    pool: _shared\n    suites:\n', 1
            ),
        )
    )
    assert [u.pool_name for u in cfg.upstreams.values()][0] == '_shared'
    cfg = load(_write(tmp_path, CONFIG))
    assert [(u.pool_name, u.unit) for u in cfg.upstreams.values()] == [
        (u.unit, u.unit) for u in cfg.upstreams.values()
    ]


def test_pool_is_a_plain_name(tmp_path):
    "A pool name with a slash would reach into another namespace"
    with pytest.raises(ConfigError, match='pool'):
        load(
            _write(
                tmp_path,
                CONFIG.replace(
                    '    suites:\n', '    pool: a/b\n    suites:\n', 1
                ),
            )
        )


@pytest.mark.parametrize(
    'value, rate',
    [
        (None, None),
        ('300M', 300_000_000),
        ('1.5G', 1_500_000_000),
        ('64k', 64_000),
        (5000000, 5_000_000),
    ],
)
def test_download_rate(tmp_path, value, rate):
    "Bytes per second, with an optional SI suffix; none means no limit"
    text = CONFIG if value is None else f'download_rate: {value}\n' + CONFIG
    assert load(_write(tmp_path, text)).download_rate == rate


@pytest.mark.parametrize('value', ['fast', '0', '-5M'])
def test_download_rate_rejects_garbage(tmp_path, value):
    "Not a number, or not positive"
    with pytest.raises(ConfigError, match='download_rate'):
        load(_write(tmp_path, f'download_rate: {value}\n' + CONFIG))


def _rewrites() -> list[tuple[str, str]]:
    "The front-end rewrite rules the example's comments give"
    lines = [
        line.lstrip('#').strip() for line in EXAMPLE.read_text().splitlines()
    ]
    return [
        (pattern, target.replace('/<BUCKET>/', '', 1))
        for pattern, target in zip(lines, lines[1:], strict=False)
        if pattern.startswith('^/') and target.startswith('/<BUCKET>/')
    ]


def test_example():
    "aptberg.example.yaml loads, and its rewrite rules find every pool"
    cfg = load(EXAMPLE)  # keyrings are only read at fetch
    ups = cfg.upstreams
    assert ups['ubuntu-security'].pool_name == 'ubuntu'
    assert ups['debian-old'].served == 'debian'
    assert ups['kubernetes'].channels['mature'].tree == '1.35'
    assert ups['nvidia-cuda-noble'].flat
    rules = _rewrites()
    assert len(rules) == 2
    for up in ups.values():
        filename = './x.deb' if up.flat else 'pool/main/a/app/x.deb'
        url = f'/{up.served}/ch/acc/{filename.removeprefix("./")}'
        found = [re.sub(p, t, url) for p, t in rules if re.match(p, url)]
        assert found[:1] == [storage_key(up, filename)], up.name
