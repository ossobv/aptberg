"""Command line: parse, call, print

Each cmd_<command> parses its arguments, calls the modules that do the
work (sync.py for the fetch/cut pipeline, promote.py, gc.py, ...) and
prints what they found. Errors those modules raise (ERRORS) end the
command with their message and exit status 1; normal output goes to
stdout, logging to stderr.
"""

import argparse
import contextlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tqdm.contrib.logging import logging_redirect_tqdm

from . import (
    apply,
    backfill,
    config,
    diff,
    gc,
    history,
    lock,
    plan,
    pool,
    promote,
    retire,
    snapshot,
    status,
    verify,
)
from .duration import parse_duration
from .fetch import (
    FetchError,
    client,
)
from .manifest import AUTO_STAGES, Manifest, ManifestError, Ref
from .progress import human
from .release import SignatureError
from .store import IntegrityError, Store, StoreError
from .sync import Scope, Sync

log = logging.getLogger('aptberg')

MISSING_SHOWN = 50
UPSTREAMS_HELP = (
    'an upstream or a group; a member of a group means the '
    'whole group (default: all)'
)


# What a command refuses with: an error message, not a traceback.
ERRORS = (
    config.ConfigError,
    FetchError,
    SignatureError,
    ManifestError,
    pool.PoolConflict,
    snapshot.CutError,
    plan.PlanError,
    lock.LockError,
    promote.PromoteError,
    history.HistoryError,
    gc.GCError,
    retire.RetireError,
    IntegrityError,
    StoreError,
    diff.DiffError,
)


def main(argv: list[str] | None = None) -> int:
    "Entry point"
    args = _parser().parse_args(argv)
    _logging(args)
    try:
        # A log line while a progress bar is up goes through tqdm.write(),
        # which clears the bar's line first and redraws it after, instead
        # of landing mid-line after the bar's \r.
        with logging_redirect_tqdm(), _output(args):
            return args.func(args)
    except ERRORS as exc:
        log.error('%s', exc)
        return 1
    except KeyboardInterrupt:
        # Every command is idempotent, so this is just a clean exit, not
        # a traceback: uploads already made are verified, in-flight
        # ones are either finished or never happened. Rerun to resume.
        log.error('interrupted')
        return 130


def _parser() -> argparse.ArgumentParser:
    "Every option and subcommand"
    parser = argparse.ArgumentParser(prog='aptberg')
    parser.add_argument(
        '-c',
        '--config',
        type=Path,
        default=Path(os.environ.get('APTBERG_CONFIG', 'aptberg.yaml')),
        help='config file (default: $APTBERG_CONFIG or ./aptberg.yaml)',
    )
    parser.add_argument(
        '-q',
        '--quiet',
        action='store_true',
        help='no output, and log warnings and errors only; the exit code '
        'tells the rest',
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='log everything, including every HTTP and S3 request',
    )
    sub = parser.add_subparsers(dest='command', required=True)

    _add_fetch(sub)
    _add_cut(sub)
    _add_sync(sub)
    _add_apply(sub)
    _add_promote(sub)
    _add_history(sub)
    _add_gc(sub)
    _add_retire(sub)
    _add_status(sub)
    _add_diff(sub)
    _add_backfill(sub)
    _add_verify(sub)
    return parser


def _add_fetch(sub: 'argparse._SubParsersAction') -> None:
    "aptberg fetch"
    fetch = sub.add_parser(
        'fetch',
        help='fetch and verify indexes, upload the pool',
        description=(
            'Fetch and verify the indexes of every configured upstream '
            'into scratch, select the pool files, and upload those '
            '_pool/ lacks. Naming upstreams limits this to them and '
            'the upstreams whose codenames they share, since the '
            'dependency closure spans a codename; only the pool files '
            'of the named ones are uploaded.'
        ),
    )
    fetch.add_argument(
        'upstreams', nargs='*', metavar='UPSTREAM', help=UPSTREAMS_HELP
    )
    fetch.add_argument(
        '--offline',
        action='store_true',
        help='reuse the indexes in scratch instead of fetching them',
    )
    fetch.add_argument(
        '--no-pool',
        action='store_true',
        help='stop after selection; do not touch the bucket',
    )
    _fetch_opts(fetch)
    fetch.set_defaults(func=cmd_fetch)


def _add_cut(sub: 'argparse._SubParsersAction') -> None:
    "aptberg cut"
    cut = sub.add_parser(
        'cut',
        help='scratch -> _snap/, and point cur and acc at it',
        description=(
            'Cut the verified indexes in scratch into immutable snapshots '
            'and write their refs into the cur and acc manifests, which '
            'every cut keeps current (prod only moves through promote). '
            'A suite whose Release has not changed since its latest '
            "snapshot keeps that snapshot's id. Refuses a suite whose "
            'selected pool files are not all in _pool/. With --apply, '
            'also applies the manifests it wrote the refs into, so cur '
            'and acc can be run unattended from cron -- or, with --stage=cur, '
            'cur alone, without ever writing or applying acc.'
        ),
    )
    cut.add_argument(
        'upstreams', nargs='*', metavar='UPSTREAM', help=UPSTREAMS_HELP
    )
    cut.add_argument(
        '--dry-run',
        action='store_true',
        help='report what would be cut, write nothing',
    )
    _cut_opts(cut)
    cut.set_defaults(func=cmd_cut)


def _add_sync(sub: 'argparse._SubParsersAction') -> None:
    "aptberg sync"
    sync = sub.add_parser(
        'sync',
        help='fetch + cut; the cron entry point',
        description=(
            'fetch (indexes and pool), then cut. With --apply, also apply '
            'the cur and acc manifests the cut wrote to: one unattended '
            'cron job. Add --stage=cur to a job that must only ever touch '
            'cur, never acc.'
        ),
    )
    sync.add_argument(
        'upstreams', nargs='*', metavar='UPSTREAM', help=UPSTREAMS_HELP
    )
    _fetch_opts(sync)
    _cut_opts(sync)
    sync.set_defaults(func=cmd_sync)


def _add_apply(sub: 'argparse._SubParsersAction') -> None:
    "aptberg apply"
    app = sub.add_parser(
        'apply',
        help='make channel prefixes serve their manifests',
        description=(
            'Diff each manifest against its channel prefix and copy what '
            'differs from the snapshots, in phases: by-hash, canonical '
            "indexes, then the Release files. Every snapshot's signature "
            'is verified again first, and every .deb it selected must '
            'be in _pool/.'
        ),
    )
    app.add_argument(
        'manifests',
        nargs='+',
        metavar='MANIFEST',
        help='a manifest file, or UPSTREAM/PREFIX (ubuntu/acc) inside '
        'the manifests: checkout',
    )
    app.add_argument(
        '--dry-run', action='store_true', help='print the plan, change nothing'
    )
    app.add_argument('--force', action='store_true', help='break a stale lock')
    app.set_defaults(func=cmd_apply, upstreams=[])


def _add_promote(sub: 'argparse._SubParsersAction') -> None:
    "aptberg promote"
    prom = sub.add_parser(
        'promote',
        help='acc -> prod',
        description=(
            'Copy the snapshot refs of the selected suites (default: all) '
            'from the acc manifests to the prod manifests. Without '
            'upstreams, every upstream; without --channel, every channel. '
            'A group is promoted per channel, whole or not at all; each '
            'group and channel is refused or promoted on its own. Refuses '
            'a suite the acc prefix does not actually serve yet. The prod '
            'plan is built and verified before the manifest is written; '
            'committing the manifests is up to you. With --apply, also '
            'applies the prod manifests.'
        ),
    )
    prom.add_argument(
        'upstreams', nargs='*', metavar='UPSTREAM', help=UPSTREAMS_HELP
    )
    prom.add_argument(
        '--channel',
        action='append',
        dest='channels',
        metavar='CHANNEL',
        help='only upstreams with channels, only this channel (repeatable)',
    )
    prom.add_argument(
        '--suite',
        action='append',
        dest='suites',
        metavar='SUITE',
        help='only this suite (repeatable)',
    )
    _codename_opt(prom)
    prom.add_argument(
        '--ref',
        metavar='REF',
        help='promote this snapshot ([TREE/]ID) instead of what acc serves '
        "now; acc's history must show it was served; one --suite",
    )
    prom.add_argument(
        '--dry-run',
        action='store_true',
        help='show the manifest change and the plan only',
    )
    prom.add_argument(
        '--apply', action='store_true', help='also apply the prod manifests'
    )
    prom.add_argument(
        '--force',
        action='store_true',
        help='with --apply: break a stale apply lock',
    )
    prom.set_defaults(func=cmd_promote)


def _add_history(sub: 'argparse._SubParsersAction') -> None:
    "aptberg history"
    hist = sub.add_parser(
        'history',
        help='what a channel prefix served, and when',
        description=(
            'Read the append-only _history/ of a channel prefix. Without '
            '--at: every apply that changed what it served. With --at: '
            'what each suite served at that moment and since when.'
        ),
    )
    hist.add_argument(
        'prefix',
        metavar='UPSTREAM/PREFIX',
        help='e.g. ubuntu/prod or kubernetes/stable-acc',
    )
    hist.add_argument(
        '--suite',
        action='append',
        dest='suites',
        metavar='SUITE',
        help='only this suite (repeatable)',
    )
    hist.add_argument(
        '--at',
        metavar='TIME',
        help='ISO date or time (UTC unless given); a date means the end '
        "of that day; 'now' for the latest state",
    )
    hist.add_argument(
        '--verify',
        action='store_true',
        help='with --at: check the recorded digests against the snapshots',
    )
    hist.add_argument(
        '--json', action='store_true', help='the same, for a program to read'
    )
    hist.set_defaults(func=cmd_history, upstreams=[])


def _add_gc(sub: 'argparse._SubParsersAction') -> None:
    "aptberg gc"
    gcp = sub.add_parser(
        'gc',
        help=('delete what nothing serves or references (default dry run)'),
        description=(
            'Index pass: objects under <upstream>/ch/<prefix>/dists/ that '
            'no snapshot the prefix served within --grace names. Pool '
            'pass: _pool/ objects no snapshot in _snap/ names. Without '
            'prefixes, every channel prefix and the pool; with, just '
            'those prefixes. A prefix no configured channel and stage '
            'names any more is only reported, unless '
            '--drop-unconfigured. Reports only, unless --act.'
        ),
    )
    gcp.add_argument('prefixes', nargs='*', metavar='UPSTREAM/PREFIX')
    gcp.add_argument(
        '--grace',
        default='7d',
        type=_duration,
        metavar='AGE',
        help='keep index objects until this long after they '
        'stopped being served, and pool files until '
        'this long after upload (7d); never longer '
        "than retire's --grace",
    )
    gcp.add_argument(
        '--drop-unconfigured',
        action='store_true',
        help='delete all of a prefix no configured channel '
        'names any more, whatever --grace says',
    )
    gcp.add_argument(
        '--act',
        action='store_true',
        help='really delete, instead of only reporting',
    )
    gcp.add_argument(
        '--list',
        action='store_true',
        help='print every key that is (or would be) deleted',
    )
    gcp.add_argument(
        '--force',
        action='store_true',
        help='break stale locks and fetch markers',
    )
    gcp.set_defaults(func=cmd_gc, upstreams=[])


def _add_retire(sub: 'argparse._SubParsersAction') -> None:
    "aptberg retire"
    ret = sub.add_parser(
        'retire',
        help='drop snapshots from _snap/ (default dry run)',
        description=(
            'Drop one snapshot (UPSTREAM SUITE REF) or, with --unused, '
            'every snapshot the policy allows, of UPSTREAM or of every '
            'upstream. Refuses a snapshot a manifest names or a prefix '
            "served within --grace; --unused also keeps each suite's "
            'newest and anything cut within --older-than. Reports only, '
            'unless --act. Run gc afterwards to free the pool.'
        ),
    )
    ret.add_argument(
        'upstream',
        nargs='?',
        metavar='UPSTREAM',
        help='required for one snapshot; with --unused, '
        'default every upstream',
    )
    ret.add_argument('suite', nargs='?', metavar='SUITE')
    ret.add_argument('ref', nargs='?', metavar='REF', help='[TREE/]ID')
    ret.add_argument(
        '--unused',
        action='store_true',
        help='every snapshot the policy allows',
    )
    ret.add_argument(
        '--older-than',
        default='30d',
        type=_duration,
        metavar='AGE',
        help='with --unused: keep snapshots cut more recently (30d)',
    )
    ret.add_argument(
        '--grace',
        default='7d',
        type=_duration,
        metavar='AGE',
        help='keep snapshots a prefix served this recently '
        "(7d); never shorter than gc's --grace, or gc "
        'fails on the snapshots it still needs',
    )
    ret.add_argument(
        '--act',
        action='store_true',
        help='really retire, instead of only reporting',
    )
    ret.set_defaults(func=cmd_retire, upstreams=[])


def _add_status(sub: 'argparse._SubParsersAction') -> None:
    "aptberg status"
    st = sub.add_parser(
        'status',
        help='per prefix and suite: named, served, since when',
        description=(
            'One row per suite of every channel prefix (from the '
            'manifests checkout and the bucket): the ref its manifest '
            'names, the ref served according to the history, since '
            'when, the upstream date of the served snapshot, and its '
            'state. States: drift (the served Release is not the one '
            'history recorded), unrecorded (served, but no history), '
            'incomplete (a partial apply), pending (the manifest names '
            'another ref than is served: normal until the next apply), '
            'behind acc (prod serves another ref than acc: normal '
            'between promotions). Exits 1 on the first three.'
        ),
    )
    st.add_argument(
        'upstreams', nargs='*', metavar='UPSTREAM', help=UPSTREAMS_HELP
    )
    st.add_argument(
        '--json',
        action='store_true',
        help='one object per row, for monitoring',
    )
    st.set_defaults(func=cmd_status)


def _add_diff(sub: 'argparse._SubParsersAction') -> None:
    "aptberg diff"
    df = sub.add_parser(
        'diff',
        help='package-level diff between two snapshots of a suite',
        description=(
            'Compare the Packages indexes of two already-cut snapshots '
            'of the same suite: packages added, removed, or changed '
            'version, by component/architecture. Indexes are never '
            'filtered, so this is what a client would see, not just '
            'what the pool holds.'
        ),
    )
    df.add_argument('upstream', metavar='UPSTREAM')
    df.add_argument('suite', metavar='SUITE')
    df.add_argument('id_a', metavar='REF-A', help='[TREE/]ID')
    df.add_argument('id_b', metavar='REF-B', help='[TREE/]ID')
    df.add_argument(
        '--json', action='store_true', help='the same, for a program to read'
    )
    df.set_defaults(func=cmd_diff, upstreams=[])


def _add_backfill(sub: 'argparse._SubParsersAction') -> None:
    "aptberg backfill"
    bf = sub.add_parser(
        'backfill',
        help='pool files a widened filter wants for live snapshots',
        description=(
            'Apply the current filter to the snapshots that manifests '
            'name or prefixes serve (their indexes read from _snap/, '
            'verified) and upload what _pool/ lacks. The closure always '
            'spans all upstreams; naming upstreams or groups only limits '
            'the upload. Only reaches names within an index kind a '
            'snapshot already fetched: it cannot backdate deb_src onto a '
            'snapshot cut before that was turned on -- fetch/sync + '
            'cut that suite instead.'
        ),
    )
    bf.add_argument(
        'upstreams', nargs='*', metavar='UPSTREAM', help=UPSTREAMS_HELP
    )
    bf.add_argument(
        '--all-snapshots',
        action='store_true',
        help='every snapshot in _snap/, not just live ones',
    )
    bf.add_argument(
        '--override-source-url',
        action='store_true',
        help="fetch pool files from each upstream's current url: (or "
        "channel url, for the ref's tree) instead of what its "
        'snapshot recorded -- e.g. the original host is down or '
        'gone; point url: at a mirror first, then backfill with '
        'this. Every file is still checked against the hash and '
        'size its signed Release named, whatever serves it',
    )
    _fetch_opts(bf)
    bf.set_defaults(func=cmd_backfill)


def _add_verify(sub: 'argparse._SubParsersAction') -> None:
    "aptberg verify"
    ver = sub.add_parser(
        'verify',
        help='re-check published prefixes as a client would',
        description=(
            'From the bucket: signatures, every mirrored index at its '
            'canonical and by-hash path, the served Release against the '
            'history, every selected .deb in _pool/. With --url: what '
            'the front end serves, including a sample of pool files '
            'through the rewrite. Exits 1 on any error.'
        ),
    )
    ver.add_argument(
        'prefixes',
        nargs='*',
        metavar='UPSTREAM/PREFIX',
        help='default: every channel prefix',
    )
    ver.add_argument(
        '--deep',
        action='store_true',
        help="hash every index, check every pool object's recorded sha256",
    )
    ver.add_argument(
        '--url',
        metavar='BASE',
        help='check over HTTP, e.g. http://aptberg.example.com',
    )
    ver.add_argument(
        '--sample',
        type=int,
        default=20,
        metavar='N',
        help='with --url: pool files to request per suite',
    )
    ver.add_argument(
        '--verbose-errors',
        action='store_true',
        help='list every error, not just the first ten',
    )
    ver.set_defaults(func=cmd_verify, upstreams=[])


def _logging(args: argparse.Namespace) -> None:
    """INFO by default; warnings only with --quiet or --json

    --json output is for a program to read, so it gets no INFO chatter
    alongside it, on stderr or anywhere else.
    """
    if args.debug:
        level = logging.DEBUG
    elif args.quiet or getattr(args, 'json', False):
        level = logging.WARNING
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level, format='%(asctime)s %(levelname)s %(message)s'
    )
    log.setLevel(level)
    if not args.debug:
        for noisy in ('httpx', 'httpcore', 'botocore', 'boto3'):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _output(args: argparse.Namespace) -> contextlib.AbstractContextManager:
    "Where normal output goes: nowhere with --quiet"
    if not args.quiet:
        return contextlib.nullcontext()
    return contextlib.redirect_stdout(open(os.devnull, 'w'))


def _duration(text: str) -> timedelta:
    "An AGE option's value"
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _fetch_opts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='read the bucket, report what would be written, write nothing',
    )
    parser.add_argument(
        '--show-missing',
        action='store_true',
        help='list every unsatisfied dependency, not just the first '
        f'{MISSING_SHOWN}',
    )


def _codename_opt(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--codename',
        action='append',
        dest='codenames',
        metavar='CODENAME',
        help='every suite whose Release has this codename, across the '
        'group: noble means noble, noble-updates and noble-security; '
        'bookworm also means bookworm-security, -backports and '
        '-updates (repeatable; adds to --suite)',
    )


def _cut_opts(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--suite',
        action='append',
        dest='suites',
        metavar='SUITE',
        help='only this suite (repeatable)',
    )
    _codename_opt(parser)
    parser.add_argument(
        '--channel',
        action='append',
        dest='channels',
        metavar='CHANNEL',
        help='only upstreams with channels, only the tree this channel '
        'follows, only its manifest (repeatable)',
    )
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='cut even if selected pool files are missing from _pool/; '
        'they are recorded in snapshot.json',
    )
    parser.add_argument(
        '--stage',
        action='append',
        dest='stages',
        metavar='STAGE',
        choices=AUTO_STAGES,
        help="only this stage's manifest, cur or acc (repeatable; "
        'default: both); e.g. --stage=cur for a cron run that must '
        'not write or apply acc',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='also apply the cur/acc manifests the cut wrote to',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='with --apply: break a stale apply lock',
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    "aptberg fetch"
    cfg = _config(args)
    # --no-pool never touches the bucket, so it skips the store too.
    job = Sync(cfg, None if args.no_pool else _store(cfg), _scope(args))
    job.load(args.offline)
    _report(job.selection, args.show_missing)
    return _upload(job, args)


def cmd_cut(args: argparse.Namespace) -> int:
    "aptberg cut"
    cfg = _config(args)
    job = Sync(cfg, _store(cfg), _scope(args))
    job.load(offline=True)
    return _cut(job, args)


def cmd_sync(args: argparse.Namespace) -> int:
    "aptberg sync"
    cfg = _config(args)
    job = Sync(cfg, _store(cfg), _scope(args))
    job.load(offline=False)
    _report(job.selection, args.show_missing)
    # A failed pool file does not stop the cut: cut refuses exactly the
    # suites that name it and cuts the rest.
    status = _upload(job, args)
    return _cut(job, args) or status


def cmd_apply(args: argparse.Namespace) -> int:
    "aptberg apply"
    cfg = _config(args)
    store = _store(cfg)
    manifests = [_manifest(cfg, name) for name in args.manifests]
    pool_listing = pool.list_sizes(
        store,
        [
            cfg.upstreams[m.upstream]
            for m in manifests
            if m.upstream in cfg.upstreams
        ],
    )
    plans = [_plan(cfg, store, m, pool_listing) for m in manifests]
    for p in plans:
        print('\n'.join(p.summary()))
        if not args.dry_run:
            _apply(cfg, args, store, p, 'apply')
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    "aptberg promote"
    cfg = _config(args)
    if cfg.manifests is None:
        raise config.ConfigError('promote needs manifests: in config')
    names = args.upstreams or list(cfg.upstreams)
    units = promote.units([cfg.upstreams[n] for n in names], args.channels)
    store = _store(cfg)
    prepared = promote.prepare_units(
        cfg.manifests,
        cfg.bucket,
        units,
        store,
        args.suites,
        args.codenames,
        Ref.parse(args.ref) if args.ref else None,
    )
    for unit, why in prepared.refused:
        log.error('%s: %s', unit, why)
    if prepared.unclaimed:
        raise promote.PromoteError(
            f'no acc manifest has suite {", ".join(prepared.unclaimed)}'
        )
    pool_listing = pool.list_sizes(
        store,
        [
            cfg.upstreams[p.prod.upstream]
            for group in prepared.groups
            for p in group
        ],
    )
    status = 1 if prepared.refused else 0
    for group in prepared.groups:
        status = _promote(cfg, args, store, group, pool_listing) or status
    return status


def _promote(
    cfg: config.Config,
    args: argparse.Namespace,
    store: Store,
    group: list[promote.Promotion],
    pool_listing: dict[str, int],
) -> int:
    """Plan, write and maybe apply one unit's promotions

    Every prod plan of the unit is built (and so verified) before any of
    its manifests is touched: a group is promoted whole or not at all.
    """
    plans = []
    try:
        for promotion in group:
            print('\n'.join(promotion.lines()))
            plans.append(_plan(cfg, store, promotion.prod, pool_listing))
            print('\n'.join(plans[-1].summary()))
    except plan.PlanError as exc:
        log.error('%s: %s', group[0].prod.path, exc)
        return 1
    if args.dry_run:
        return 0
    for promotion in group:
        if promotion.changes:
            promotion.prod.save()
    if args.apply:
        for promotion, p in zip(group, plans, strict=True):
            source = promotion.acc.key_prefix.rstrip('/')
            _apply(cfg, args, store, p, f'promote from {source}')
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    "aptberg history"
    cfg = _config(args)
    upstreams, prefix = _prefix(cfg, args.prefix)
    store = _store(cfg)
    events = history.events(store, prefix)
    wanted = set(args.suites or ())
    if args.at is None:
        return _history_changes(args, prefix, events, wanted)
    return _history_at(args, store, upstreams, prefix, events, wanted)


def _history_changes(
    args: argparse.Namespace,
    prefix: str,
    events: list[history.Event],
    wanted: set[str],
) -> int:
    "history without --at: every event that changed what was served"
    rows = []
    for event, changed in history.changes(events):
        changed = {
            s: d for s, d in changed.items() if not wanted or s in wanted
        }
        if not changed and wanted:
            continue
        rows.append((event, changed))
    if args.json:
        print(
            json.dumps(
                [
                    {
                        'at': e.at.isoformat(),
                        'key': e.key,
                        'note': e.note,
                        'host': e.host,
                        'complete': e.complete,
                        'changes': {
                            s: [str(o) if o else None, str(n)]
                            for s, (o, n) in d.items()
                        },
                    }
                    for e, d in rows
                ],
                indent=1,
            )
        )
        return 0
    for event, changed in rows:
        what = (
            ', '.join(
                f'{s} {o or "(new)"} -> {n}' for s, (o, n) in changed.items()
            )
            or 'no change'
        )
        flag = '' if event.complete else '  INCOMPLETE'
        print(
            f'{event.at:%Y-%m-%d %H:%M:%S} UTC  {what}  '
            f'[{event.note} on {event.host}]{flag}'
        )
    if not rows:
        print(f'{prefix}: no history')
    return 0


def _history_at(
    args: argparse.Namespace,
    store: Store,
    upstreams: dict[str, config.Upstream],
    prefix: str,
    events: list[history.Event],
    wanted: set[str],
) -> int:
    "history --at: what each suite served then, and since when"
    at = None if args.at == 'now' else history.parse_time(args.at)
    live = {
        s: v
        for s, v in history.state_at(events, at).items()
        if not wanted or s in wanted
    }
    problems = history.verify(store, upstreams, live) if args.verify else {}
    if args.json:
        print(
            json.dumps(
                {
                    s: {
                        'ref': str(v.served.ref),
                        'since': v.since.isoformat(),
                        'release_sha256': v.served.release_sha256,
                        'event': v.event.key,
                        'problem': problems.get(s),
                    }
                    for s, v in sorted(live.items())
                },
                indent=1,
            )
        )
    else:
        when = f'{at:%Y-%m-%d %H:%M:%S} UTC' if at else 'now'
        print(f'{prefix} at {when}:')
        for suite, v in sorted(live.items()):
            state = ''
            if args.verify:
                state = f'  {problems.get(suite, "verified")}'
            print(
                f'  {suite}: {v.served.ref} since '
                f'{v.since:%Y-%m-%d %H:%M:%S} UTC  '
                f'sha256 {v.served.release_sha256[:16]}{state}'
            )
        if not live:
            print('  nothing served')
    return 1 if problems else 0


def cmd_diff(args: argparse.Namespace) -> int:
    "aptberg diff"
    cfg = _config(args)
    if args.upstream not in cfg.upstreams:
        raise config.ConfigError(f'unknown upstream {args.upstream!r}')
    store = _store(cfg)
    ref_a, ref_b = Ref.parse(args.id_a), Ref.parse(args.id_b)
    result = diff.diff_suite(store, args.upstream, args.suite, ref_a, ref_b)
    if args.json:
        print(
            json.dumps(
                {
                    head: {
                        'added': d.added,
                        'removed': d.removed,
                        'changed': {n: list(v) for n, v in d.changed.items()},
                    }
                    for head, d in result.items()
                },
                indent=1,
                sort_keys=True,
            )
        )
        return 0
    if not result:
        print(
            f'{args.suite}: no package differences between {ref_a} and {ref_b}'
        )
        return 0
    for head, d in sorted(result.items()):
        print(f'{head}:')
        rows = [(n, None, v) for n, v in d.added.items()]
        rows += [(n, v, None) for n, v in d.removed.items()]
        rows += [(n, o, v) for n, (o, v) in d.changed.items()]
        for name, old, new in sorted(rows, key=lambda r: r[0]):
            print(f'  {name} {old or "(none)"} -> {new or "(none)"}')
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    "aptberg gc"
    cfg = _config(args)
    store = _store(cfg)
    prefixes = _prefixes(cfg, store, args.prefixes)
    verb = 'deleted' if args.act else 'would delete'
    for n, (upstreams, prefix) in enumerate(prefixes, 1):
        log.info('%s: index pass (%d/%d)', prefix, n, len(prefixes))
        if args.act:
            with lock.exclusive(store, lock.prefix_key(prefix), args.force):
                found = gc.index_pass(
                    store,
                    upstreams,
                    prefix,
                    args.grace,
                    drop_unconfigured=args.drop_unconfigured,
                )
                gc.collect(store, found)
        else:
            found = gc.index_pass(
                store,
                upstreams,
                prefix,
                args.grace,
                drop_unconfigured=args.drop_unconfigured,
            )
        _gc_report(found, verb, args.list)
    if not args.prefixes:
        log.info('pool pass')
        found = gc.pool_pass(
            store, args.grace, cfg.upstreams, force=args.force
        )
        if args.act:
            gc.collect(store, found)
        _gc_report(found, verb, args.list)
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    "aptberg backfill"
    cfg = _config(args)
    store = _store(cfg)
    if args.all_snapshots:
        refs = backfill.all_refs(store, cfg)
    else:
        refs = backfill.live_refs(store, cfg, datetime.now(timezone.utc))
    if not refs:
        print('no snapshots to backfill')
        return 0
    cfg.scratch.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='backfill-', dir=cfg.scratch) as work:
        job = Sync(cfg, store, _scope(args))
        job.use(
            backfill.load(
                store, cfg, refs, Path(work), args.override_source_url
            )
        )
        _report(job.selection, args.show_missing)
        return _upload(job, args)


def cmd_verify(args: argparse.Namespace) -> int:
    "aptberg verify"
    cfg = _config(args)
    store = _store(cfg)
    prefixes = _prefixes(cfg, store, args.prefixes)
    # One listing of every pool involved, not one per prefix: a pool can
    # be millions of keys, and many prefixes share it.
    pool_listing = pool.list_etags(
        store, [up for upstreams, _ in prefixes for up in upstreams.values()]
    )
    failed = False
    for n, (upstreams, prefix) in enumerate(prefixes, 1):
        log.info('%s: verifying (%d/%d)', prefix, n, len(prefixes))
        if args.url:
            reports = verify.verify_http(
                client(cfg.concurrency, cfg.user_agent, cfg.download_rate),
                args.url,
                upstreams,
                prefix,
                store,
                args.sample,
                pool_listing,
            )
        else:
            reports = verify.verify_prefix(
                store,
                upstreams,
                prefix,
                args.deep,
                cfg.concurrency,
                pool_listing,
            )
        print(prefix.rstrip('/'))
        for report in reports:
            print(f'  {report.line()}')
            errors = (
                report.errors if args.verbose_errors else report.errors[:10]
            )
            for error in errors:
                print(f'    ERROR {error}')
            if len(errors) < len(report.errors):
                print(f'    ... {len(report.errors) - len(errors)} more')
            for warning in report.warnings[:10]:
                print(f'    warning {warning}')
            failed |= bool(report.errors)
    return 1 if failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    "aptberg status"
    cfg = _config(args)
    store = _store(cfg)
    now = datetime.now(timezone.utc)
    rows = []
    for name in args.upstreams or list(cfg.upstreams):
        upstream = cfg.upstreams[name]
        foreign = frozenset(
            s
            for n, u in cfg.served_upstreams(upstream.served).items()
            if n != name
            for s in u.suites
        )
        rows += status.cells(
            store, upstream, cfg.manifests, cfg.bucket, now, foreign
        )
    if args.json:
        print(json.dumps([c.to_dict(now) for c in rows], indent=1))
    else:
        _status_table(rows, now)
    return 1 if any(c.problem for c in rows) else 0


def _status_table(rows: list[status.Cell], now: datetime) -> None:
    "status as a table, one row per suite of a prefix"
    head = (
        'PREFIX',
        'SUITE',
        'MANIFEST',
        'SERVED',
        'SINCE',
        'AGE',
        'UPSTREAM DATE',
        'STATE',
    )
    table = [head] + [
        (
            c.prefix.rstrip('/'),
            c.suite,
            str(c.manifest or '-'),
            str(c.served or '-'),
            f'{c.since:%Y-%m-%d %H:%M}' if c.since else '-',
            _age(now - c.since) if c.since else '-',
            c.release_date or '-',
            ', '.join(c.states) or 'ok',
        )
        for c in rows
    ]
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    for r in table:
        print(
            '  '.join(
                v.ljust(w) for v, w in zip(r, widths, strict=True)
            ).rstrip()
        )


def _age(delta) -> str:
    days = delta.total_seconds() / 86400
    return f'{days:.1f}d' if days >= 1 else f'{days * 24:.1f}h'


def cmd_retire(args: argparse.Namespace) -> int:
    "aptberg retire"
    cfg = _config(args)
    if args.unused and (args.suite or args.ref):
        raise retire.RetireError('give either SUITE REF or --unused')
    if not args.unused and not args.ref:
        raise retire.RetireError('give UPSTREAM SUITE REF, or --unused')
    if args.upstream is None:
        upstreams = list(cfg.upstreams.values())
    elif args.upstream in cfg.upstreams:
        upstreams = [cfg.upstreams[args.upstream]]
    else:
        raise config.ConfigError(f'unknown upstream {args.upstream!r}')
    store = _store(cfg)
    status = 0
    for upstream in upstreams:
        status = _retire(cfg, args, store, upstream) or status
    return status


def _retire(
    cfg: config.Config,
    args: argparse.Namespace,
    store: Store,
    upstream: config.Upstream,
) -> int:
    "retire for one upstream"
    now = retire.now_utc()
    manifests, served = retire.context(
        store, upstream, cfg.manifests, cfg.bucket, args.grace, now
    )
    snaps = retire.snapshots(store, upstream)
    if args.unused:
        kept = retire.policy(snaps, args.older_than, now)
    else:
        ref = Ref.parse(args.ref)
        snaps = [s for s in snaps if s.suite == args.suite and s.ref == ref]
        if not snaps:
            raise retire.RetireError(
                f'no snapshot {args.suite} {ref} of {upstream.name}'
            )
        kept = {}
    verb = 'retired' if args.act else 'would retire'
    status = 0
    for snap in snaps:
        why = retire.blockers(snap, manifests, served)
        if snap.root in kept:
            why.append(kept[snap.root])
        state = '' if snap.complete else ', incomplete'
        if why:
            if not args.unused:
                status = 1
            print(f'keep    {snap.root} ({"; ".join(why)})')
            continue
        if args.act:
            retire.retire(store, snap)
        print(
            f'{verb:<7} {snap.root} ({len(snap.keys)} objects, '
            f'{human(snap.size)}{state})'
        )
    return status


def _gc_report(found: gc.Collection, verb: str, show: bool) -> None:
    young = f', {found.young} too young' if found.young else ''
    print(
        f'{found.scope}: {verb} {len(found.dead)} '
        f'({human(found.dead_bytes)}), kept {found.kept}{young}'
    )
    if found.unconfigured:
        print(
            '  no configured channel names this prefix; '
            '--drop-unconfigured deletes all of it'
        )
    if show:
        for key in sorted(found.dead):
            print(f'  {key}')


def _prefixes(
    cfg: config.Config, store: Store, names: list[str]
) -> list[tuple[dict[str, config.Upstream], str]]:
    """The named UPSTREAM/PREFIXes, or every channel prefix in the bucket

    Each with every upstream serving there, as _prefix gives it.
    """
    if names:
        return [_prefix(cfg, name) for name in names]
    served_paths = sorted({up.served for up in cfg.upstreams.values()})
    return [
        (cfg.served_upstreams(served), f'{served}/ch/{name}/')
        for served in served_paths
        for name in store.list_dirs(f'{served}/ch/')
    ]


def _prefix(
    cfg: config.Config, text: str
) -> tuple[dict[str, config.Upstream], str]:
    "UPSTREAM/PREFIX -> every upstream serving there, and the key prefix"
    served, _, name = text.partition('/')
    if not name or '/' in name:
        raise config.ConfigError(
            f'{text!r}: expected UPSTREAM/PREFIX, like ubuntu/prod'
        )
    upstreams = cfg.served_upstreams(served)
    if not upstreams:
        raise config.ConfigError(f'unknown upstream {served!r}')
    return upstreams, f'{served}/ch/{name}/'


def _manifest(cfg: config.Config, name: str) -> Manifest:
    path = Path(name)
    if not path.is_file():
        if cfg.manifests is None:
            raise ManifestError(
                f'{name}: not a file, and no manifests: in config'
            )
        path = cfg.manifests / f'{name}.yaml'
    owner = cfg.upstreams.get(path.parent.name)
    served = owner.served if owner else path.parent.name
    return Manifest.read(path, cfg.bucket, served)


def _config(args: argparse.Namespace) -> config.Config:
    cfg = config.load(args.config)
    if args.upstreams:
        # Naming one upstream of a group, or the group, means all of it.
        named = list(args.upstreams)
        args.upstreams = cfg.expand(named)
        extra = [n for n in args.upstreams if n not in named]
        if extra:
            log.info('with the rest of its group: %s', ', '.join(extra))
    return cfg


def _scope(args: argparse.Namespace) -> Scope:
    "What fetch, cut, sync or backfill was asked to work on"
    return Scope(
        tuple(args.upstreams),
        tuple(getattr(args, 'suites', None) or ()),
        tuple(getattr(args, 'codenames', None) or ()),
        tuple(getattr(args, 'channels', None) or ()),
    )


def _upload(job: Sync, args: argparse.Namespace) -> int:
    "Upload the pool files _pool/ lacks, and say how that went"
    selection = job.pool_selection()
    print(
        f'pool: {len(selection.items)} files, {human(selection.total_bytes)}'
    )
    if getattr(args, 'no_pool', False):
        return 0
    result = job.upload(args.dry_run)
    again = (
        f', {result.redo} of them uploaded multipart before'
        if result.redo
        else ''
    )
    print(
        f'pool: {result.present} present, {result.todo} to upload '
        f'({human(result.todo_bytes)}{again})'
    )
    if not args.dry_run:
        print(
            f'pool: uploaded {result.uploaded} '
            f'({human(result.uploaded_bytes)})'
        )
    for key, why in result.failed:
        log.error('%s: %s', key, why)
    return 1 if result.failed else 0


def _cut(job: Sync, args: argparse.Namespace) -> int:
    "Cut, say what each suite came to, and with --apply apply it"
    status = 0
    touched: dict[Path, Manifest] = {}
    for cut in job.cut(args.allow_missing, args.dry_run, args.stages):
        if cut.error:
            log.error('%s: %s', cut.name, cut.error)
            status = 1
            continue
        result = cut.result
        state = (
            'unchanged'
            if result.reused
            else 'would cut'
            if args.dry_run
            else f'cut, {result.uploaded} files uploaded'
        )
        extra = (
            f', {len(result.missing)} pool files missing'
            if result.missing
            else ''
        )
        print(f'{cut.name}: {result.ref} ({state}{extra})')
        for manifest in cut.manifests:
            print(f'  {manifest.path}')
            touched[manifest.path] = manifest
    if args.apply and touched:
        manifests = list(touched.values())
        upstreams = [job.cfg.upstreams[m.upstream] for m in manifests]
        _apply_touched(
            job.cfg, args, job.store, manifests, job.present(upstreams)
        )
    return status


def _apply_touched(
    cfg: config.Config,
    args: argparse.Namespace,
    store: Store,
    manifests: list[Manifest],
    pool_listing: dict[str, int] | None = None,
) -> None:
    "Build and apply the plan of every manifest the cut wrote to"
    if pool_listing is None:
        pool_listing = pool.list_sizes(
            store, [cfg.upstreams[m.upstream] for m in manifests]
        )
    for manifest in manifests:
        p = _plan(cfg, store, manifest, pool_listing)
        print('\n'.join(p.summary()))
        _apply(cfg, args, store, p, 'cut')


def _plan(
    cfg: config.Config,
    store: Store,
    manifest: Manifest,
    pool_listing: dict[str, int],
) -> plan.Plan:
    "The plan of a manifest, its snapshots verified"
    upstream = cfg.upstreams.get(manifest.upstream)
    if upstream is None:
        raise config.ConfigError(
            f'{manifest.path}: upstream {manifest.upstream!r} is not '
            f'configured'
        )
    log.info(
        '%s: verifying and diffing against the prefix',
        manifest.key_prefix.rstrip('/'),
    )
    return plan.build(
        manifest, upstream, store, cfg.concurrency, pool=pool_listing
    )


def _apply(
    cfg: config.Config,
    args: argparse.Namespace,
    store: Store,
    p: plan.Plan,
    note: str,
) -> None:
    "Apply a plan, and say what that did"
    done = apply.run(p, store, cfg.concurrency, force=args.force, note=note)
    print(
        f'{p.prefix}: applied, {done} ops'
        if done
        else f'{p.prefix}: up to date'
    )


def _store(cfg: config.Config) -> Store:
    "A store, checked: every command that touches S3 fails fast on it"
    store = Store(
        cfg.bucket, cfg.endpoint, cfg.region, cfg.concurrency, cfg.user_agent
    )
    store.check()
    return store


def _report(selection: pool.Selection, show_all: bool) -> None:
    for codename, result in sorted(selection.closures.items()):
        hard = sorted(result.missing_hard())
        soft = sorted(result.missing.get('Recommends', ()))
        print(
            f'{codename}: {len(result.wanted)} packages wanted; '
            f'{len(result.absent)} listed names not in {codename}; '
            f'{len(hard)} unsatisfied dependencies, '
            f'{len(soft)} unsatisfied recommends'
        )
        if result.excluded:
            back = len(result.excluded & result.wanted)
            print(
                f'  {len(result.excluded)} names soft excluded, {back} '
                f'of them kept because something depends on them'
            )
        if result.blocked:
            broken = sorted(result.broken_hard())
            print(
                f'  {len(result.blocked)} names hard excluded; '
                f'{len(broken)} dependencies broken by that'
            )
            for dep in broken if show_all else broken[:MISSING_SHOWN]:
                print(f'  broken: {dep}')
            if not show_all and len(broken) > MISSING_SHOWN:
                print(
                    f'  ... {len(broken) - MISSING_SHOWN} more '
                    f'(--show-missing)'
                )
        shown = hard if show_all else hard[:MISSING_SHOWN]
        for clause in shown:
            print(f'  missing: {clause}')
        if len(shown) < len(hard):
            print(f'  ... {len(hard) - len(shown)} more (--show-missing)')


if __name__ == '__main__':
    sys.exit(main())
