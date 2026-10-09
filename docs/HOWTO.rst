Running aptberg
===============

This is the operator's guide: why aptberg exists, the words it uses, how
to set it up, and the jobs that keep it running. The README has the
short version, `DESIGN.rst <DESIGN.rst>`_ the reasons behind the
layout, `FAQ.rst <FAQ.rst>`_ the odd jobs, and ``aptberg <command>
--help`` every option.

.. contents::
   :local:
   :depth: 1


Why
---

aptberg is one apt mirror that does three jobs at once.

**A plain mirror.** The ``cur`` stage follows upstream continuously.
Hosts that just want a nearby, fast copy of Ubuntu, Debian or a vendor
repository point at ``cur`` and get ordinary mirror behaviour.

**A history of every package.** Every index aptberg ever served stays
available as an immutable snapshot in ``_snap/``. Every change to what
a channel prefix serves is logged in ``_history/``, and the manifests
that say which snapshot is served where live in git. You can always
answer "what did prod serve last Tuesday at 03:00?" and get those exact
indexes back, together with the ``.deb`` files they name.

**Tested updates.** Acceptance hosts follow ``acc``, production hosts
follow ``prod``, and ``prod`` only ever receives a snapshot that ``acc``
has served. A production host never sees a package that an acceptance
host has not been offered first, for at least as long as the schedule
between cut and promote.

All three come from one copy of every file. ``cur`` already holds the
``.deb`` files, so cutting a new ``acc`` costs little more than copying
index files.

Upstream ``Release`` and ``Packages`` files are served byte for byte,
signatures included. Clients keep verifying against the upstream's own
key; aptberg has no signing key of its own.


Glossary
--------

**upstream**
    One configured source repository: a URL, a keyring and the suites
    to mirror (``ubuntu``, ``ubuntu-security``, ``zabbix``). Its *name*
    is its key in ``aptberg.yaml``.

**suite**
    A distribution in an upstream, the ``noble-updates`` in ``dists/
    noble-updates/``. A flat repository has one suite, named after the
    upstream.

**codename, family**
    The codename a suite's ``Release`` carries. Its *family* folds the
    pockets in: ``noble-security`` and ``noble-updates`` belong to
    ``noble``. Dependencies are resolved per family, across upstreams;
    ``--codename=noble`` selects the whole family.

**group**
    Upstreams that are fetched, cut and promoted together
    (``ubuntu`` and ``ubuntu-security``). Naming one member, or the
    group, on the command line means all of them.

**unit**
    What is handled together: a group, or an upstream without one.
    For promote, a unit is one group on one channel.

**pool**
    Where an upstream's ``.deb`` files are stored: ``_pool/<pool>/``.
    It defaults to the group's name, else the upstream's. Every file is
    stored once, however many snapshots name it.

**path**
    Where an upstream is served: ``<path>/ch/...``. It defaults to the
    name. ``path:`` lets an upstream with its own keyring and URL (an
    EOL suite moved to archive.debian.org) be served where another
    one is.

**channel, tree**
    For upstreams whose version is in the URL, not the suite name
    (kubernetes 1.31, zabbix 7.0), a *channel* (``stable``) is a named
    pointer to one version line, its *tree* (``1.31``). Moving a
    channel to a new version is a config change; hosts keep the same
    sources line.

**stage**
    ``cur`` (follows upstream), ``acc`` (acceptance) or ``prod``
    (production).

**channel prefix**
    What a host's sources line names: ``<path>/ch/<stage>``, or
    ``<path>/ch/<channel>-<stage>`` for an upstream with channels.
    Written ``ubuntu/acc`` or ``kubernetes/stable-prod`` on the command
    line.

**snapshot, id, ref**
    A *snapshot* is a suite's verified indexes and signatures, frozen
    under ``_snap/<upstream>/[<tree>/]<suite>/<id>/``. Its *id* is the
    date plus a letter (``20261009a``). A *ref* is ``[<tree>/]<id>``:
    ``20261009a``, or ``1.31/20261009a`` for an upstream with channels.

**manifest**
    A small YAML file in the manifests checkout,
    ``<upstream>/[<channel>-]<stage>.yaml``, mapping each suite to the
    ref its channel prefix should serve. It says what *should* be
    served, not what is.

**history**
    ``_history/<path>/ch/<prefix>/``: one event per apply that changed
    what a prefix serves. It says what *was* served, and since when.

**fetch**
    Download and verify every upstream's indexes into the scratch
    directory, select the ``.deb`` files the filters want (with their
    dependencies), and upload the ones ``_pool/`` lacks.

**cut**
    Freeze what is in scratch into new snapshots, and write their refs
    into the ``cur`` and ``acc`` manifests. A suite that has not moved
    keeps its snapshot. A cut serves nothing by itself.

**apply**
    Make a channel prefix serve what its manifest names: copy the
    snapshot's indexes there, and the ``Release`` files last. Rolling
    back is applying an older ref.

**sync**
    fetch, then cut, in one run.

**promote**
    Copy refs from the ``acc`` manifests into the ``prod`` manifests,
    but only those ``acc`` actually serves.

**retire**
    Delete snapshots nothing names or served recently.

**gc**
    Delete index objects no prefix served recently, and ``.deb`` files
    no snapshot names. The only command that deletes content.

**--apply, --act**
    ``--apply`` means "and then also publish": cut, sync and promote
    also apply the manifests they wrote. ``--act`` means "really
    delete": retire and gc only report without it.

How the commands relate::

    upstream --fetch--> scratch, _pool/
                           |
                          cut ---------> _snap/ snapshot
                           |
                           v
                  cur.yaml, acc.yaml --apply--> ch/cur, ch/acc
                           |
                        promote (only what ch/acc serves)
                           v
                       prod.yaml ------apply--> ch/prod


Setup
-----

**Install.** aptberg needs Python 3.10 or newer and ``gpgv``::

    pip install .            # from a checkout of this repository

**Configure.** Copy ``aptberg.example.yaml`` to ``aptberg.yaml`` and
adapt it. Its comments explain every key, and in particular how name,
group, pool and path relate. Relative paths resolve against the
config's directory. aptberg reads ``./aptberg.yaml``, or what ``-c`` or
``$APTBERG_CONFIG`` names.

**Keyrings.** Each upstream needs its signing key as a binary keyring
(``gpg --dearmor`` an ASCII-armoured key). Every fetch and every apply
checks signatures against it.

**Manifests.** ``manifests:`` points at a git checkout of its own;
aptberg writes manifests there and never commits. Commit ``acc`` and
``prod`` manifests; they are the record of what was decided. The
``cur`` manifests change on every sync and can be left out of git::

    # .gitignore of the manifests repository
    /*/cur.yaml
    /*/*-cur.yaml

**S3.** Credentials are not in ``aptberg.yaml``: boto3 finds them in
its usual places (``AWS_PROFILE``, ``AWS_ACCESS_KEY_ID`` and friends, or
``~/.aws/``). The config holds ``bucket:``, ``endpoint:`` and
``region:``. aptberg needs list, get, head, put, copy and delete on the
bucket. Enable versioning (or object lock) on it, so ``_history/``
cannot be rewritten. Creating the bucket and its keys is outside this
guide.

**Front end.** Clients reach the bucket through an HTTP front end that
serves ``/<path>/ch/`` and rewrites pool requests to ``_pool/``. The
rewrite rules, and the case where the first one does not fit, are in
the comments of ``aptberg.example.yaml``. Deny ``/_*``: everything else
aptberg keeps starts with an underscore. Setting up the front end is
outside this guide.

**First run.** Check each layer before the next::

    aptberg fetch --no-pool          # indexes and selection; no bucket
    aptberg fetch --dry-run          # also lists _pool/: tests credentials
    aptberg sync --apply --stage=cur # uploads the pool: hours, the first time
    aptberg cut --apply --stage=acc  # acc gets the same snapshots
    aptberg promote --apply          # and prod
    aptberg status
    aptberg verify --url=http://aptberg.example.com

``fetch --no-pool`` prints, per family, how many packages the filters
want and which dependencies they cannot satisfy; adjust the filters
until that looks right. Every command is idempotent: an interrupted run
continues where it stopped when it is run again.


Jobs
----

Three scheduled jobs keep the stages moving. They share scratch and the
bucket, so run them one at a time: give every aptberg job the same
``flock``. aptberg's own locks refuse a second apply of the same prefix,
but not every overlap.

Cron runs without your login environment: set the AWS variables (or
``HOME`` for ``~/.aws/``) in the crontab or the script. Use ``-q``: a
quiet run prints nothing and logs only warnings and errors, so cron
mails only when something needs a look. The exit code is 1 on any
error.

Continuous: cur
~~~~~~~~~~~~~~~

::

    aptberg -q sync --apply --stage=cur

fetches every upstream, uploads the new ``.deb`` files, cuts every
suite that moved, writes the ``cur`` manifests and applies them.
``--stage=cur`` keeps it away from ``acc``: this job never changes what
acceptance hosts see. Run it as often as upstream changes matter to
you, every hour or two say; ``flock -n`` skips a run while the
previous one is still going::

    15 */2 * * *  aptberg  flock -n /run/lock/aptberg aptberg -q sync --apply --stage=cur

Weekly: promote, then cut acc
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    aptberg -q promote --apply
    aptberg -q cut --apply --stage=acc

``promote`` moves what ``acc`` has served all week to ``prod``, and
applies ``prod``. It promotes only what the ``acc`` prefix actually
serves, so a cut that never reached ``acc`` cannot reach ``prod``. A
group that cannot be promoted is reported and skipped; the others are
still promoted.

``cut --stage=acc`` then gives ``acc`` the newest snapshots, from the
indexes the last sync left in scratch, and applies them. It cuts
nothing new for a suite ``cur`` already has: the suite keeps its
snapshot id, and ``acc`` gets the very snapshot ``cur`` serves.

Promote first: the snapshot reaching ``prod`` then has had a full week
on ``acc``. The other order is safe (promote never takes what ``acc``
does not serve) but gives the new ``acc`` no soak time at all.

Both write manifests; commit them afterwards. A script for both::

    #!/bin/sh
    # /usr/local/sbin/aptberg-weekly
    rc=0
    aptberg -q promote --apply || rc=1
    aptberg -q cut --apply --stage=acc || rc=1
    cd /srv/aptberg-manifests &&
        git add -A && { git diff --cached --quiet ||
        git commit -qm 'weekly: promote to prod, cut acc'; }
    exit $rc

::

    0 6 * * 1  aptberg  flock /run/lock/aptberg /usr/local/sbin/aptberg-weekly

Without ``-n``, ``flock`` waits for a running sync to finish.

Housekeeping
~~~~~~~~~~~~

Snapshots and pool files are never deleted by the jobs above. Monthly,
say::

    aptberg -q retire --unused --act
    aptberg -q gc --act

``retire --unused`` drops snapshots no manifest names, no prefix served
within ``--grace`` (7d), and that are not their suite's newest or
younger than ``--older-than`` (30d). ``gc`` then deletes the index
objects no prefix served within its ``--grace`` (7d) and the ``.deb``
files no remaining snapshot names. Run both without ``--act`` first, to
see what would go. Keep retire's ``--grace`` at least gc's.


Applying by hand
----------------

The jobs above apply as they go. Without ``--apply``, cut and promote
only write manifests, and you decide when a prefix changes::

    aptberg cut ubuntu --stage=acc              # writes ubuntu*/acc.yaml
    aptberg diff ubuntu noble-updates 20261002a 20261009a
    aptberg apply ubuntu/acc ubuntu-security/acc --dry-run
    aptberg apply ubuntu/acc ubuntu-security/acc

``apply`` takes ``UPSTREAM/PREFIX`` (``ubuntu/acc``,
``kubernetes/stable-prod``) or a manifest file. ``--dry-run`` prints the
plan: how many files each phase copies. Before planning, it verifies
every snapshot's signature again and refuses a manifest whose ``.deb``
files are not all in ``_pool/``. A prefix serves the old snapshot until
the last phase, which writes the ``Release`` files suite by suite.

Promote can be narrowed the same way, and checked first::

    aptberg promote ubuntu --codename=noble --dry-run
    aptberg promote ubuntu --codename=noble     # writes ubuntu*/prod.yaml
    aptberg apply ubuntu/prod ubuntu-security/prod

**Rolling back** is applying an older ref. Find the one you want, point
the manifest at it, apply, and commit::

    aptberg history ubuntu/prod --suite=noble-updates
    $EDITOR /srv/aptberg-manifests/ubuntu/prod.yaml
    aptberg apply ubuntu/prod

Or let promote do the editing; it accepts any ref ``acc`` has served::

    aptberg promote ubuntu --suite=noble-updates --ref=20261002a --apply

A rolled-back ``prod`` stays put until the next promote; a rolled-back
``acc`` until the next cut, which points it at the newest snapshot again.

**What is served, and since when**::

    aptberg status ubuntu
    aptberg history ubuntu/prod --at=2026-10-06T03:00 --verify

``status`` shows per prefix and suite the ref the manifest names, the
ref served, and since when. ``pending`` (the manifest is ahead of the
prefix) and ``behind acc`` are normal between jobs.


Clients
-------

A host's sources line names a channel prefix::

    deb http://aptberg.example.com/ubuntu/ch/prod noble main restricted universe
    deb http://aptberg.example.com/ubuntu/ch/prod noble-updates main restricted universe
    deb http://aptberg.example.com/ubuntu-security/ch/prod noble-security main restricted universe
    deb http://aptberg.example.com/kubernetes/ch/stable-prod anydist main

Acceptance hosts use ``acc`` instead of ``prod``; hosts that want a
plain mirror use ``cur``. Keep ``signed-by=`` pointing at the upstream's
own key: the ``Release`` files are upstream's.
``deb-src`` works the same way, for upstreams that mirror ``Sources``
(``deb_src:``, on by default).

Every ``acc`` and ``prod`` host also needs::

    # /etc/apt/apt.conf.d/99-phased-updates
    Update-Manager::Always-Include-Phased-Updates true;
    APT::Get::Always-Include-Phased-Updates true;

Ubuntu phases updates: each host decides from its own machine-id
whether it takes an update yet. Without this, an ``acc`` host can skip
an update that a ``prod`` host later takes, and ``prod`` would install
something ``acc`` never ran.


Monitoring
----------

::

    aptberg status --json
    aptberg -q verify
    aptberg -q verify --url=http://aptberg.example.com

``status`` exits 1 when a prefix serves something its history does not
explain: changed behind aptberg's back (``drift``), never recorded
(``unrecorded``), or left half applied (``incomplete``). ``--json``
prints one object per row, for a monitoring check to read (not with
``-q``, which silences all output).

``verify`` checks every channel prefix in the bucket: signatures, every
index at its canonical and by-hash path, the served ``Release`` against
the history, and every ``.deb`` in ``_pool/`` against its listing.
``--deep`` also hashes everything (slow). With ``--url`` it checks what
clients see through the front end, including a sample of pool files
through the rewrite. It exits 1 on any error.
