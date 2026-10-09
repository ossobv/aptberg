"""Package selection by name: seed names plus dependency closure

A filter selects package names: the seeds (include patterns minus soft
excludes) and everything they depend on, soft excluded or not. Hard
excluded names are never selected; a dependency that can only be met by
one is broken on purpose and reported. Every stanza carrying a selected
name is mirrored, all versions and architectures; the indexes themselves
are never touched.

Deliberate simplifications:
- version constraints are ignored; every version the index holds counts
- of "a | b", an alternative that is already selected satisfies the
  clause, else the first alternative present in the index is taken
- a virtual package resolves to a provider already selected, else to the
  alphabetically first provider
"""

import re
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

HARD = ('Pre-Depends', 'Depends')
FOLLOW = (*HARD, 'Recommends')

_NAME = re.compile(r'\s*([^\s(\[<:,|]+)')


def relations(value: str) -> list[list[str]]:
    """Split a relation field into clauses of alternative package names

    "a (>= 1), b:any | c [amd64]" -> [["a"], ["b", "c"]]
    """
    out = []
    for clause in value.split(','):
        alts = []
        for alt in clause.split('|'):
            match = _NAME.match(alt)
            if match:
                alts.append(match.group(1))
        if alts:
            out.append(alts)
    return out


GLOB_CHARS = '*?['


def is_glob(pattern: str) -> bool:
    "Whether a pattern is a glob rather than a literal name"
    return any(c in pattern for c in GLOB_CHARS)


def matches(
    names: Iterable[str], patterns: Sequence[str]
) -> dict[str, set[str]]:
    """The names each fnmatch pattern matches

    Each pattern only scans the names sharing its literal prefix, so
    thousands of patterns against a hundred thousand names stay cheap.
    """
    ordered = sorted(names)
    out = {}
    for pattern in patterns:
        head = re.split(r'[*?\[]', pattern, maxsplit=1)[0]
        found = set()
        for i in range(bisect_left(ordered, head), len(ordered)):
            name = ordered[i]
            if not name.startswith(head):
                break
            if fnmatchcase(name, pattern):
                found.add(name)
        out[pattern] = found
    return out


def matching(names: Iterable[str], patterns: Sequence[str]) -> set[str]:
    "The names matching any of the fnmatch patterns"
    return set().union(*matches(names, patterns).values())


class Index:
    "Stanzas by package name, and who provides which virtual name"

    def __init__(self) -> None:
        self.stanzas: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.provides: dict[str, set[str]] = defaultdict(set)

    def add(self, stanza: dict[str, str]) -> None:
        "Add one Packages stanza"
        name = stanza['Package']
        self.stanzas[name].append(stanza)
        for alts in relations(stanza.get('Provides', '')):
            for virtual in alts:
                self.provides[virtual].add(name)

    def resolve(
        self,
        name: str,
        chosen: set[str],
        blocked: frozenset[str] | set[str] = frozenset(),
    ) -> str | None:
        "The real, unblocked package that satisfies name, or None"
        if name in self.stanzas and name not in blocked:
            return name
        providers = self.provides.get(name, set()) - blocked
        if not providers:
            return None
        return next(
            (p for p in sorted(providers) if p in chosen), min(providers)
        )

    def known(self, name: str) -> bool:
        "Whether the index has name, as a package or a virtual one"
        return name in self.stanzas or name in self.provides


@dataclass
class Closure:
    "Outcome of a closure: what to mirror and what could not be found"

    wanted: set[str] = field(default_factory=set)
    # seed names this index does not have (normal: the include list spans
    # codenames and carries old versioned names)
    absent: set[str] = field(default_factory=set)
    # relation field -> unsatisfiable clauses, e.g. "Depends" -> {"a | b"}
    missing: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    # relation field -> "dependent: clause" left unmet because only a
    # hard excluded package could satisfy it
    broken: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
    # names a soft exclude dropped; those also in wanted came back
    excluded: set[str] = field(default_factory=set)
    # names a hard exclude matched
    blocked: set[str] = field(default_factory=set)

    def missing_hard(self) -> set[str]:
        "Unsatisfied Pre-Depends and Depends: these break installs"
        return set().union(*(self.missing.get(f, set()) for f in HARD))

    def broken_hard(self) -> set[str]:
        "Pre-Depends and Depends broken by a hard exclude"
        return set().union(*(self.broken.get(f, set()) for f in HARD))


def closure(
    index: Index,
    seeds: Iterable[str],
    follow: Sequence[str] = FOLLOW,
    blocked: frozenset[str] | set[str] = frozenset(),
) -> Closure:
    """Resolve seeds and follow the given relation fields transitively

    Blocked (hard excluded) names are treated as absent, except that a
    relation only they could satisfy is recorded as broken rather than
    missing.
    """
    out = Closure(blocked=set(blocked))
    seeds = sorted(set(seeds) - set(blocked))
    # Real seed packages count as chosen from the start, so an
    # alternative or provider that is listed anyway is preferred.
    out.wanted.update(s for s in seeds if s in index.stanzas)
    for seed in seeds:
        name = index.resolve(seed, out.wanted, blocked)
        if name is None:
            out.absent.add(seed)
        else:
            out.wanted.add(name)
    todo = sorted(out.wanted, reverse=True)
    done: set[str] = set()
    while todo:
        name = todo.pop()
        if name in done:
            continue
        done.add(name)
        for stanza in index.stanzas[name]:
            for fld in follow:
                for alts in relations(stanza.get(fld, '')):
                    target = _pick(index, alts, out.wanted, blocked)
                    if target is None and any(map(index.known, alts)):
                        out.broken[fld].add(f'{name}: {" | ".join(alts)}')
                    elif target is None:
                        out.missing[fld].add(' | '.join(alts))
                    elif target not in done:
                        out.wanted.add(target)
                        todo.append(target)
    return out


def _pick(
    index: Index,
    alts: list[str],
    chosen: set[str],
    blocked: frozenset[str] | set[str],
) -> str | None:
    resolved = [index.resolve(a, chosen, blocked) for a in alts]
    for name in resolved:
        if name is not None and name in chosen:
            return name
    return next((n for n in resolved if n is not None), None)
