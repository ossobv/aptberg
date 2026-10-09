aptberg design
==============

How aptberg stores and serves an apt mirror with moving snapshots, and
why. `HOWTO.rst <HOWTO.rst>`_ has the glossary and the day-to-day jobs;
this document has the choices behind them.

.. contents::
   :local:
   :depth: 1


Three decisions
---------------

**Never re-sign upstream trees.** Every upstream's ``InRelease``,
``Release`` and ``Release.gpg`` are served verbatim, signed by that
upstream's key. The mirror is then a provable subset of upstream: clients
verify it with a key aptberg does not hold. aptberg checks the same
signature itself, with a pinned keyring, at every fetch and again at every
apply.

**Snapshot ids go in object paths, never in suite names.** The ``Packages``
files must stay byte-identical to what the signed ``Release`` hashes, so
aptberg cannot rename suites, merge them, or add a component to someone
else's distribution. The indirection lives in the path instead: a snapshot
is an immutable prefix, a channel prefix serves one, and a client's sources
line never changes.

**Storage is S3.** No local disk and no stateful server in the serving
path. Switching a suite to a new snapshot ends with copying one object, its
``InRelease``, which is atomic; ``by-hash`` makes that enough (see
`The flip`_).


Channels and stages
-------------------

A host picks one *channel prefix* per upstream: ``<path>/ch/<stage>``, or
``<path>/ch/<channel>-<stage>`` for an upstream with channels.

**Channels** pin a version line, and only where the suite name does not
already. Ubuntu's suites carry their release in the name (``noble``,
``jammy``), so ``ubuntu`` has no channels and its three prefixes hold every
release side by side. Kubernetes and etcd publish one tree per minor
version under the same suite name (``anydist``), so they have channels:
in ``aptberg.example.yaml``, ``early``, ``general``, ``mature`` and
``vintage``, each naming a *tree* (``1.37`` ... ``1.34``) and its URL.
Moving a channel to a new version is a config change; hosts keep the same
sources line. An upstream with a single version line, like zabbix 7.0,
just has that version in its ``url:``.

**Stages** are ``cur``, ``acc`` and ``prod``, per upstream. ``cur`` follows
upstream with no gate, for hosts that want a plain mirror. ``acc`` is the
first gated deployment. ``prod`` only ever receives a snapshot ``acc`` has
served. ``cut`` keeps ``cur`` and ``acc`` current; ``prod`` moves only
through ``promote``. The stages need not share a schedule: ``--stage=cur``
keeps a cron job away from ``acc``.

The guarantee is about the index, not about what apt installs from it:
Ubuntu's phased updates let each host decide from its own machine-id
whether to take an update yet. ``acc`` and ``prod`` hosts need the
``Always-Include-Phased-Updates`` setting in HOWTO.rst, or an ``acc`` host
may skip an update a ``prod`` host later takes.


Name, group, pool and path
--------------------------

Four config values place an upstream. Keeping them apart is what lets one
bucket hold unrelated archives safely:

name
    Its key in ``aptberg.yaml``. Its snapshots live in ``_snap/<name>/``,
    its manifests in ``<manifests>/<name>/``. It is what has a keyring, a
    URL and a schedule of its own.

group
    Upstreams fetched, cut and promoted together (``ubuntu`` and
    ``ubuntu-security``). Their dependencies resolve together anyway, and
    cut refuses a group whose indexes come from different fetch runs, so a
    security update is never cut against a base it was not fetched with.

pool
    Where the ``.deb`` files go: ``_pool/<pool>/``. It defaults to the
    group's name, else the upstream's own. Within one pool a file is stored
    once; across pools nothing is shared (see `The pool`_).

path
    Where it is served: ``<path>/ch/...``. It defaults to the name.
    ``debian-old`` (jessie, moved to archive.debian.org with another key)
    keeps its own name, keyring and snapshots, but is served at
    ``debian/ch/...``, so hosts change nothing when a suite goes EOL. Two
    upstreams sharing a path may not both list a suite.


Bucket layout
-------------

::

    s3://aptberg.example.com/
      _pool/                              one directory per pool
        ubuntu/main/o/openssl/openssl_3.0.13-0ubuntu3.4_amd64.deb
        debian/main/b/bash/bash_5.2.21-2+deb13u1_amd64.deb
        nvidia-cuda-noble/cuda-toolkit_12.6.2-1_amd64.deb
      _snap/                              immutable, never served
        ubuntu/noble/20260901a/dists/noble/...
        ubuntu/noble-updates/20260901b/dists/noble-updates/...
        ubuntu-security/noble-security/20260909c/...
        kubernetes/1.35/anydist/20261005a/dists/anydist/...
        zabbix/noble/20260801a/dists/noble/...
      _history/ubuntu/ch/prod/<time>-<host>-<pid>.json
      _lock/ubuntu/ch/acc                 exists while apply runs
      ubuntu/ch/cur/dists/noble/InRelease
      ubuntu/ch/cur/dists/noble/main/binary-amd64/by-hash/SHA256/<sha256>
      ubuntu/ch/cur/dists/noble/main/binary-amd64/Packages.gz
      ubuntu/ch/{acc,prod}/...
      ubuntu-security/ch/{cur,acc,prod}/...
      kubernetes/ch/{early,general,mature,vintage}-{cur,acc,prod}/...
      zabbix/ch/{cur,acc,prod}/...

Everything aptberg keeps for itself starts with an underscore; upstream
names may not. Only ``<path>/ch/`` is served, so the front end can deny
``/_*`` outright.

**Snapshot ids** are the date plus a letter: ``20260909a``, ``20260909b``,
up to 26 a day, sorting in time order. A suite whose ``Release`` has not
changed since its latest snapshot keeps that id. A **ref** is
``[<tree>/]<id>``: upstreams with channels add the tree.

A snapshot holds ``dists/`` (the indexes aptberg mirrors, and the signature
files) and three lists of its own: ``snapshot.json``, what the cut
verified, written last, so a snapshot without it is incomplete;
``filenames.gz``, every file its indexes name; ``selected.gz``, the files
the filters kept.


The pool
--------

``Filename:`` in ``Packages`` is relative to the sources line's URI and
cannot change without breaking the signature, so a host on
``ubuntu/ch/prod`` asks for ``ubuntu/ch/prod/pool/main/o/openssl/...``. An
HTTP rewrite in front of the bucket serves those from ``_pool/``::

    /<path>/ch/<prefix>/pool/<file>   ->  /_pool/<pool>/<file>
    /<path>/ch/<prefix>/<file>.deb    ->  /_pool/<pool>/<file>.deb   (flat)

The rewrite does not read ``aptberg.yaml``, so the mapping from path to
pool is kept by hand. The rules in ``aptberg.example.yaml`` take the path
up to its first dash as the pool (``ubuntu-security`` -> ``_pool/ubuntu/``),
which fits its upstreams but not every config. A mismatch fails quietly --
objects sit at keys nothing asks for -- so ``aptberg verify --url``
requests a sample of pool files through the front end, and a test keeps the
example's rules and pools in step. The price of the rewrite: the bucket is
no longer servable by plain S3 alone.

**Pools are not shared by default.** A ``name_version_arch`` is unique only
within one archive's release process. Debian and Ubuntu routinely publish
the same one with different bytes (`FINDINGS.rst <FINDINGS.rst>`_), so
sharing a pool across upstreams is an assumption to state with ``pool:``.
Within a pool, **an object is never overwritten with different bytes**:
older snapshots name the existing bytes, so fetch refuses and names both
sources.

**Flat repositories** (NVIDIA's CUDA repositories) have no ``dists/``:
``Release``, ``Packages`` and the ``.deb`` files share one directory, and
``Packages`` names ``./x.deb``. Nothing can move without breaking the
vendor's signature, so ``apply`` puts their ``Release`` and indexes at the
top of the channel prefix, and the second rewrite rule serves the ``.deb``
files beside them. Clients use ``deb <prefix> ./``.


The flip
--------

Ubuntu and Debian set ``Acquire-By-Hash: yes``, so apt fetches indexes from
``.../by-hash/SHA256/<sha256>``: content-addressed, immutable objects.
``apply`` makes a channel prefix serve a manifest in phases, each complete
before the next:

1. Verify every snapshot's signature again from ``_snap/``, and its
   ``snapshot.json`` against the signed ``Release``. Check that every
   ``.deb`` it selected is in ``_pool/``; nothing is copied there.
2. Copy the new ``by-hash`` objects into the prefix. New keys only; nothing
   is overwritten.
3. Copy the canonical index paths (``Packages.gz`` and so on). These
   overwrite, and only clients with by-hash disabled read them.
4. Copy ``InRelease``, ``Release`` and ``Release.gpg``, suite by suite,
   and delete a signature file the new snapshot lacks.

A client holding the old ``InRelease`` still resolves its old hashes,
because nothing before step 4 removed anything; one fetching after step 4
gets the new ones. There is no window in which the index is inconsistent.
Old ``by-hash`` objects stay until ``gc`` collects them (see
`Retention`_). Without ``Acquire-By-Hash``, an ``apt update`` running
during step 3 can fail once, and a rerun succeeds.

Every apply that changes what a prefix serves appends an event to
``_history/<path>/ch/<prefix>/``: per suite, the ref and the sha256 of the
``InRelease`` (or ``Release``) served. A partial apply records only the
suites whose step 4 finished. Enable bucket versioning or object lock, so
the history cannot be rewritten.

Rolling back is applying an older ref. Nothing was destroyed.


Manifests
---------

One YAML file per channel prefix, in a git repository of its own that the
config names::

    # <manifests>/ubuntu/prod.yaml
    prefix: s3://aptberg.example.com/ubuntu/ch/prod
    suites:
      jammy:          20260715a
      noble:          20260901a
      noble-updates:  20260901b

    # <manifests>/kubernetes/mature-prod.yaml
    prefix: s3://aptberg.example.com/kubernetes/ch/mature-prod
    suites:
      anydist:        1.35/20261005a

A channel's ref names its tree, because the channel moves to another tree
on a version bump and a committed manifest must keep meaning what it
meant.

``cut`` writes fresh refs into the ``cur`` and ``acc`` manifests;
``promote`` copies refs from ``acc`` to ``prod``. Both apply what they
wrote only with ``--apply``. Manifests say what *should* be served;
``_history/`` says what *was*, and ``aptberg status`` shows the two side
by side. ``git log`` on a ``prod`` manifest answers "when did this move,
and to what"; ``cur`` changes on every sync and is best left out of git.

**Promote takes only what acc serves.** The ``acc`` manifest is no proof:
cut writes it before anyone applies it. So promote compares each suite's
snapshot ``Release`` files with what the ``acc`` prefix serves, and refuses
a suite where they differ. ``--ref`` promotes an older snapshot instead, if
``acc``'s history shows it served it. A group is promoted per channel, all
members or none; different groups are independent.


Fetch and the dependency closure
--------------------------------

Fetch takes, per configured component and architecture, the ``Packages``
(``binary-<arch>`` and ``binary-all``), ``Sources`` unless ``deb_src:
false``, ``i18n``, ``cnf``, ``dep11`` and ``Contents-<arch>`` -- each
verified against the signed ``Release`` -- and leaves out the rest the
``Release`` names. Then it uploads the files the filters select and
``_pool/`` lacks.

The indexes stay whole and signed; filtering only decides which ``.deb``
files are mirrored. A package left out is a 404 at install time, not a
verification failure, as with any partial mirror.

The filters' closure follows dependencies per **family**: the suites
sharing a ``Codename:``, with pockets folded in (``noble-updates`` and
``noble-security`` belong to ``noble``), across upstreams. A suite whose
codename no filtered upstream uses (an ``anydist`` tree) joins every
family. So a ``missing:`` line in a family that could never carry the
package -- a PPA's ``anydist`` package depending on ``zabbix-agent``,
reported under a family zabbix does not publish for -- is not a hole in
the mirror.

A filter is evaluated per fetch: a package gaining a dependency pulls in
more, and a pattern that stops matching drops something. ``aptberg diff``
shows what a new snapshot changed; after widening a filter, ``aptberg
backfill`` uploads what live snapshots now want.


Retention
---------

``.deb`` files cannot be fetched again once upstream drops them, so a
snapshot keeps everything it names for as long as it exists: rolling back
to it, or promoting it after ``acc`` moved on, always works. ``gc`` is the
only command that deletes content, in two passes:

- **Index pass**, per channel prefix: objects no snapshot the prefix served
  within ``--grace`` (7d, from ``_history/``) names. The grace runs from
  when an object stopped being served. These are copies of ``_snap/``
  content, so a rollback recreates them.
- **Pool pass**, over the whole bucket: ``.deb`` files no snapshot in
  ``_snap/`` names, each resolved through its own upstream's pool. It
  refuses while a fetch is uploading, and keeps files younger than the
  grace: fetch uploads them before the cut that names them.

Pool files therefore go only after ``retire`` drops the snapshots naming
them, which makes retire the real retention knob. It never drops a
snapshot a manifest names or a prefix served within its ``--grace``; with
``--unused`` it also keeps each suite's newest and any cut within
``--older-than`` (30d). Its grace must be at least gc's, or gc finds a
snapshot gone that it still needs.

An S3 lifecycle rule cannot say "except what is still referenced", so
none of this is expressed as one.


Emergency updates
-----------------

An urgent fix still goes cut -> acc -> prod, with the soak compressed:
sync ``ubuntu-security``, ``aptberg diff`` the new snapshot, apply
``ubuntu-security/acc``, test, and promote with
``--suite=noble-security``. Every other suite stays where it is.

Signed upstream indexes cannot be edited, so a single package cannot be
patched into a frozen snapshot. Publish it in a PPA of your own instead
(the same ``.deb``, or a rebuild with a higher version), mirror that PPA
like any upstream, and remove it once the regular suite catches up.


What upstreams must provide
---------------------------

- **Acquire-By-Hash**, for the flip to have no window. Most do; check
  third-party repositories.
- **No Valid-Until**, or snapshots expire. Ubuntu's suites have none
  (checked 2026-09-20). Debian's security suite has 7 days: serving a frozen
  one longer needs ``Check-Valid-Until: no`` on the clients, which turns off
  apt's protection against replayed indexes.
- **InRelease, or Release with Release.gpg.** aptberg handles both.


What this does not solve
------------------------

- Nothing here says a snapshot is *good*. The time on ``acc`` and your
  tests there are the gate.
- Channels and stages drift independently: nothing makes a prefix move.
  ``aptberg status --json`` gives the age of what each one serves
  (``age_seconds``), for an alert.
- The retirement policy -- the age, and how often -- is yours to choose.
  Keeping a particular snapshot longer is an open question
  (`TODO.rst <TODO.rst>`_).
