from aptberg.filter import Index, closure, matches, relations


def _index(*stanzas: dict[str, str]) -> Index:
    idx = Index()
    for stanza in stanzas:
        idx.add(stanza)
    return idx


def test_relations():
    "Versions, arch qualifiers and restrictions are stripped"
    assert relations(
        'libc6 (>= 2.38), perl:any | awk [amd64], foo <!nocheck>,'
    ) == [['libc6'], ['perl', 'awk'], ['foo']]
    assert relations('') == []


def test_closure_follows_depends():
    "Transitive Depends and Pre-Depends are followed; others are not"
    idx = _index(
        {'Package': 'a', 'Depends': 'b'},
        {'Package': 'b', 'Pre-Depends': 'c'},
        {'Package': 'c', 'Suggests': 'd'},
        {'Package': 'd'},
    )
    assert closure(idx, ['a']).wanted == {'a', 'b', 'c'}


def test_closure_recommends_is_a_setting():
    "Recommends is followed by default and can be switched off"
    idx = _index({'Package': 'a', 'Recommends': 'b'}, {'Package': 'b'})
    assert closure(idx, ['a']).wanted == {'a', 'b'}
    assert closure(idx, ['a'], follow=['Depends']).wanted == {'a'}


def test_closure_every_version():
    "Dependencies of every version of a name are followed"
    idx = _index(
        {'Package': 'kubelet', 'Version': '1.31.1', 'Depends': 'old'},
        {'Package': 'kubelet', 'Version': '1.31.2', 'Depends': 'new'},
        {'Package': 'old'},
        {'Package': 'new'},
    )
    assert closure(idx, ['kubelet']).wanted == {'kubelet', 'old', 'new'}


def test_closure_alternatives():
    "First present alternative, unless another one is already wanted"
    idx = _index(
        {'Package': 'a', 'Depends': 'gone | x | y'},
        {'Package': 'b', 'Depends': 'x | y'},
        {'Package': 'x'},
        {'Package': 'y'},
    )
    assert closure(idx, ['a']).wanted == {'a', 'x'}
    assert closure(idx, ['y', 'b']).wanted == {'b', 'y'}


def test_closure_provides():
    "A virtual package resolves to a provider"
    idx = _index(
        {'Package': 'a', 'Depends': 'mail-transport-agent'},
        {'Package': 'postfix', 'Provides': 'mail-transport-agent'},
        {'Package': 'exim4', 'Provides': 'mail-transport-agent (= 1)'},
    )
    assert closure(idx, ['a']).wanted == {'a', 'exim4'}
    assert closure(idx, ['postfix', 'a']).wanted == {'a', 'postfix'}


def test_closure_versioned_parent():
    "An unversioned metapackage pulls in its versioned package"
    idx = _index(
        {'Package': 'python3', 'Depends': 'python3.12 (>= 3.12.3-0)'},
        {'Package': 'python3.12'},
        {'Package': 'python3.13'},
    )
    assert closure(idx, ['python3']).wanted == {'python3', 'python3.12'}


def test_closure_absent_and_missing():
    "Unknown seeds are absent; unknown dependencies are missing"
    idx = _index(
        {'Package': 'a', 'Depends': 'nope', 'Recommends': 'nah | nix'}
    )
    out = closure(idx, ['a', 'linux-image-2.6.32-58'])
    assert out.wanted == {'a'}
    assert out.absent == {'linux-image-2.6.32-58'}
    assert out.missing_hard() == {'nope'}
    assert out.missing['Recommends'] == {'nah | nix'}


def test_matches():
    "Per pattern matches, literal prefixes narrowing the scan"
    names = [
        'linux-generic',
        'linux-image-6.8.0-45-generic',
        'vim',
        'libnvidia-gl',
        'nvidia-prime',
    ]
    assert matches(
        names,
        ['linux-*[0-9].[0-9]*.[0-9]*-[0-9]*', '*nvidia*', 'vim', 'emacs*'],
    ) == {
        'linux-*[0-9].[0-9]*.[0-9]*-[0-9]*': {'linux-image-6.8.0-45-generic'},
        '*nvidia*': {'libnvidia-gl', 'nvidia-prime'},
        'vim': {'vim'},
        'emacs*': set(),
    }


def test_closure_blocked():
    "Blocked names are skipped for alternatives, else reported broken"
    idx = _index(
        {'Package': 'a', 'Depends': 'bad | good, bad', 'Recommends': 'gone'},
        {'Package': 'bad'},
        {'Package': 'good'},
    )
    out = closure(idx, ['a', 'bad'], blocked={'bad'})
    assert out.wanted == {'a', 'good'}
    assert out.broken_hard() == {'a: bad'}
    assert out.missing['Recommends'] == {'gone'}
