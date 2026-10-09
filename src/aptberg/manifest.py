"""Manifests: which snapshot each suite of a channel prefix serves

One YAML file per channel prefix, in its own git repository:

    <manifests>/<upstream>/<stage>.yaml             ubuntu/acc.yaml
    <manifests>/<upstream>/<channel>-<stage>.yaml   kubernetes/stable-acc.yaml

    prefix: s3://aptberg.example.com/kubernetes/ch/stable-acc
    suites:
      anydist: 1.31/20260101a

A suite maps to a snapshot reference: the bare id for an upstream
without channels, <tree>/<id> for one with. The tree is stored because
the channel-to-tree mapping in the config moves on a version bump, while
a manifest must keep meaning what it meant when it was committed.

aptberg edits these files; committing them is up to the caller.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ID = re.compile(r'\d{8}[a-z]')
STAGES = ('cur', 'acc', 'prod')
# Stages cut keeps current on every run, no promotion involved: cur for
# the always-current mirror old-style clients point straight at, acc
# for the acc -> prod pipeline.
AUTO_STAGES = ('cur', 'acc')


class ManifestError(Exception):
    "A manifest or snapshot reference is malformed"


@dataclass(frozen=True, order=True)
class Ref:
    "A snapshot reference: [<tree>/]<id>"

    tree: str | None
    id: str

    @classmethod
    def parse(cls, text: str) -> 'Ref':
        "Parse 20260909a or 1.31/20260909a"
        tree, _, snap = str(text).rpartition('/')
        if not ID.fullmatch(snap) or (_ and not tree):
            raise ManifestError(f'not a snapshot reference: {text!r}')
        return cls(tree or None, snap)

    def __str__(self) -> str:
        return f'{self.tree}/{self.id}' if self.tree else self.id


def prefix_name(channel: str | None, stage: str) -> str:
    "The channel prefix name: acc, or stable-acc"
    if stage not in STAGES:
        raise ManifestError(f'unknown stage {stage!r}')
    return f'{channel}-{stage}' if channel else stage


@dataclass
class Manifest:
    "One channel prefix and the snapshot each of its suites serves"

    path: Path
    prefix: str
    suites: dict[str, Ref] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        root: Path,
        bucket: str,
        name: str,
        served: str,
        channel: str | None,
        stage: str,
    ) -> 'Manifest':
        """Load a manifest; a missing file is an empty manifest

        name is the upstream's own identity, used only for the manifest
        file's location on disk. served is where it is actually
        published (config.Upstream.served: path or name, wherever a
        suite this upstream owns is not served under its own name).
        """
        stage_name = prefix_name(channel, stage)
        path = Path(root) / name / f'{stage_name}.yaml'
        prefix = f's3://{bucket}/{served}/ch/{stage_name}'
        if not path.exists():
            return cls(path, prefix)
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}
        if raw.get('prefix', prefix) != prefix:
            raise ManifestError(
                f'{path}: prefix {raw["prefix"]!r}, expected {prefix!r}'
            )
        suites = {
            str(k): Ref.parse(v) for k, v in (raw.get('suites') or {}).items()
        }
        return cls(path, prefix, suites)

    @classmethod
    def read(cls, path: Path, bucket: str, served: str) -> 'Manifest':
        """Load an existing manifest file by path

        The upstream is the directory name, the prefix name the file
        stem: .../kubernetes/stable-acc.yaml. served is as in load().
        """
        path = Path(path)
        if not path.is_file():
            raise ManifestError(f'{path}: no such manifest')
        channel, _, stage = path.stem.rpartition('-')
        return cls.load(
            path.parent.parent,
            bucket,
            path.parent.name,
            served,
            channel or None,
            stage,
        )

    @classmethod
    def every(
        cls, root: Path, bucket: str, name: str, served: str
    ) -> list['Manifest']:
        "Every manifest of an upstream in the checkout, by file name"
        return [
            cls.read(path, bucket, served)
            for path in sorted((root / name).glob('*.yaml'))
        ]

    @property
    def upstream(self) -> str:
        "The upstream this manifest belongs to"
        return self.path.parent.name

    @property
    def key_prefix(self) -> str:
        "The channel prefix as an object key prefix: ubuntu/ch/acc/"
        return self.prefix.split('/', 3)[3] + '/'

    def save(self) -> None:
        "Write the manifest, suites sorted, atomically"
        width = max((len(s) for s in self.suites), default=0) + 1
        lines = [f'prefix: {self.prefix}', 'suites:']
        lines += [
            f'  {suite + ":":<{width}} {ref}'
            for suite, ref in sorted(self.suites.items())
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text('\n'.join(lines) + '\n')
        os.replace(tmp, self.path)
