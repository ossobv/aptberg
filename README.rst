aptberg
=======

Apt mirror with moving snapshots, stored in S3.

aptberg fetches signed upstream apt repositories, verifies them against a
pinned keyring and cuts immutable snapshots. It then points channel prefixes
(``cur``, ``acc``, ``prod``) at those snapshots. Upstream ``Release`` and
``Packages`` files are served byte-identical, so clients verify them against
the original upstream key. A filter only limits which pool files are
mirrored.

A host's sources list then points at a channel prefix::

    deb http://aptberg.example.com/ubuntu/ch/prod noble main restricted universe
    deb http://aptberg.example.com/kubernetes/ch/stable-prod anydist main

One mirror does three jobs:

- ``cur`` is a plain mirror, kept current continuously;
- every index ever served stays available as a snapshot, with a history
  of what was served where and when;
- ``prod`` only ever receives snapshots that ``acc`` has served, so
  acceptance hosts always see an update first.

Three jobs keep it running::

    aptberg sync --apply --stage=cur    # continuously: fetch, cut, serve cur
    aptberg promote --apply             # weekly: acc -> prod
    aptberg cut --apply --stage=acc     # weekly, after promote: cur -> acc

`docs/HOWTO.rst <docs/HOWTO.rst>`_ explains why, defines the terms, and
walks through setup, these jobs, applying by hand, client configuration
(``acc`` and ``prod`` hosts need a phased-updates setting) and
monitoring. `docs/DESIGN.rst <docs/DESIGN.rst>`_ has the design: the
bucket layout, the pool rewrite, the flip, retention.
`docs/FAQ.rst <docs/FAQ.rst>`_ has the rest: removing a renamed
upstream, and pinning third-party repositories on the hosts.
`docs/FINDINGS.rst <docs/FINDINGS.rst>`_ records the upstream quirks
behind design choices, and `docs/TODO.rst <docs/TODO.rst>`_ the open
questions.


Usage
-----

Copy ``aptberg.example.yaml`` to ``aptberg.yaml``, and adapt it.
Relative paths in the config resolve against its directory. Then::

    aptberg fetch --no-pool     # fetch and verify indexes, report selection
    aptberg fetch --dry-run     # also list the bucket: what would upload
    aptberg fetch               # upload missing pool files to _pool/
    aptberg fetch zabbix        # only zabbix's pool files
    aptberg sync ubuntu         # ubuntu and the rest of its group
    aptberg cut ubuntu --codename=noble  # noble, noble-updates, noble-security
    aptberg cut                 # scratch -> _snap/, update cur and acc manifests
    aptberg sync --apply        # fetch + cut + apply cur and acc, for cron
    aptberg sync --apply --stage=cur   # cur only, for cron; never touches acc
    aptberg apply ubuntu/cur --dry-run   # the plan for a channel prefix
    aptberg apply ubuntu/acc    # publish what manifests/ubuntu/acc.yaml says
    aptberg promote --apply     # everything: acc -> prod, then apply
    aptberg promote ubuntu --suite=noble-security   # only these suites
    aptberg promote kubernetes --channel=stable     # only this channel
    aptberg promote k8s --channel=stable            # a whole group
    aptberg promote ubuntu --codename=noble         # the noble family
    aptberg promote ubuntu --suite=noble --ref=20260908a   # an older acc one
    aptberg status              # every prefix and suite: named, served, since
    aptberg status --json       # for monitoring; exit 1 on trouble
    aptberg verify              # every prefix: ETags vs the signed MD5s
    aptberg verify ubuntu/prod --deep                # hash every index too
    aptberg verify ubuntu/prod --url=http://aptberg.example.com # as apt sees it
    aptberg history ubuntu/prod                     # what changed, when
    aptberg history ubuntu/prod --at=2026-09-14T03:00 --verify
    aptberg backfill --dry-run  # after widening a filter: what live
                               # snapshots now want that _pool/ lacks
    aptberg backfill
    aptberg retire ubuntu --unused   # snapshots the policy would drop
    aptberg retire ubuntu --unused --act
    aptberg retire --unused --act    # the same, for every upstream
    aptberg gc --list           # what would be deleted (default dry run)
    aptberg gc --act            # delete it

Requests to upstreams and to the bucket carry the User-Agent
``aptberg/<version>``, plus ``(<contact>)`` when the config sets
``contact:``, so an upstream mirror knows whom to ask about the traffic.


Development
-----------

Requires Python 3.10+ and ``gpgv`` on the path (the tests also need
``gpg``)::

    make setup
    . .venv/bin/activate
    make test
    make cov        # tests with line and branch coverage, also htmlcov/
    make fmt        # flake8, ruff and the ASCII check
    make fmtfix     # ruff check --fix, ruff format
