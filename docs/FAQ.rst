aptberg FAQ
===========

*Tips and tricks that did not fit anywhere else.*

.. contents::
   :local:
   :depth: 1


I renamed an upstream. How do I remove the old one?
---------------------------------------------------

Say ``cilium-tools`` was renamed to ``cilium`` in ``aptberg.yaml``, and
``gc`` now fails with::

    ERROR _snap/cilium-tools/anydist/20260924a/filenames.gz: upstream
    'cilium-tools' is not configured (removing an upstream loses its
    pool namespace); retire its snapshots first, or restore its
    aptberg.yaml entry

A rename is not a rename to aptberg. The upstream name is the key of
everything the old one left in the bucket:

- ``_snap/cilium-tools/...``: its snapshots;
- ``_pool/cilium-tools/...``: its .debs (the pool namespace is the name,
  or the group, or ``pool:``);
- ``cilium-tools/ch/<stage>/...`` and ``_history/cilium-tools/...``: what
  it served, and the record of it.

The new ``cilium`` starts empty: fetch and cut download and upload
everything again into ``_pool/cilium/``. Nothing is shared with the old
one, so the old files are just garbage now.

``gc`` refuses to run while ``_snap/cilium-tools/`` exists but the config
no longer says where those snapshots' files live: it would have to guess
the pool, and a wrong guess deletes live files. ``retire`` needs the same
config entry. So the entry has to come back, temporarily.

Procedure
~~~~~~~~~

1. Put the old entry back in ``aptberg.yaml``, next to the new one, with
   the same ``url``, ``suites``, ``group`` and ``pool`` as before. It must
   be enabled.

2. Remove the old manifests (``git rm -r cilium-tools/`` in the manifests
   checkout, and push). ``retire`` refuses any snapshot a manifest in the
   checkout still names.

3. Only if the old upstream was ever applied, so that ``cilium-tools/ch/``
   exists in the bucket: remove that served tree. ``retire`` refuses any
   snapshot a prefix served within ``--grace``, and the snapshot currently
   served is always inside it. Nothing in aptberg removes a served tree;
   use your S3 tool, for example::

       aws s3 rm --recursive s3://BUCKET/cilium-tools/

   Leave ``_history/`` alone. aptberg never deletes it, and once the
   prefix is gone nothing reads it.

4. Retire every snapshot, as a dry run first::

       aptberg retire cilium-tools --unused --older-than=0s
       aptberg retire cilium-tools --unused --older-than=0s --act

   ``--unused`` always keeps the newest complete snapshot of each suite
   ("newest of its suite"), even with ``--older-than=0s``, so the last one
   of each suite must be named explicitly, as many times as there are
   suites::

       aptberg retire cilium-tools anydist 20260924a
       aptberg retire cilium-tools anydist 20260924a --act

   (Use ``TREE/ID`` as the ref for an upstream with channels.) Run
   ``--unused`` again afterwards: it lists whatever is left.

5. Free the pool::

       aptberg gc
       aptberg gc --act

   The ``.deb`` files of ``_pool/cilium-tools/`` are no longer named by any
   snapshot and go once older than ``--grace`` (7d). A shorter
   ``--grace=1h`` does not wait for that, but it also shortens how long
   every channel prefix keeps the index files it served last: a host still
   holding last hour's InRelease may then fail an ``apt update`` once.
   Never pass ``gc`` a longer ``--grace`` than ``retire`` had.

6. Remove the old entry from ``aptberg.yaml`` again. ``gc`` now passes
   without it, as ``_snap/cilium-tools/`` is empty.

Notes
~~~~~

- Hosts that still have ``.../cilium-tools/ch/prod`` in their sources
  will get 404s from step 3 on. Point them at ``cilium`` first.
- Step 5 is the only step that frees space, and the only one that cannot
  be undone. The dry runs of ``retire`` and ``gc`` tell you what would go.


How do I stop a third-party repo from supplying anything but its own packages?
------------------------------------------------------------------------------

With apt preferences on the hosts (noble and up). Say only ``zabbix-*``
may come from the Zabbix repo, and win over Ubuntu's own ``zabbix-*``::

    # /etc/apt/preferences.d/zabbix.pref
    Explanation: zabbix-* may come from Zabbix, and beats Ubuntu's
    Package: zabbix-*
    Pin: release o=Zabbix
    Pin-Priority: 600

    Explanation: nothing else may come from there
    Package: *
    Pin: release o=Zabbix
    Pin-Priority: -1

Per package, apt uses the first stanza that matches: an exact name before
any glob, globs in file order. So the allow stanza goes before the
``Package: *`` one. The ``Pin-Priority`` is below 0 for "never", 500 is
the default, 990 is the target release and above 1000 apt will even
downgrade for it (don't).

What to pin on
~~~~~~~~~~~~~~

- Use ``Pin: release o=...,l=...,a=...,n=...,c=...``. aptberg serves each
  Release byte-identical to upstream's, so these are upstream's own
  values, and the same pref file works on ``cur``, ``acc`` and ``prod``
  hosts. Read them off a host that has the repo configured:
  ``apt-cache policy zabbix-agent`` prints a line like
  ``release v=...,o=<Origin>,a=...,n=noble,l=<Label>,c=main``.
- ``Pin: origin "host"`` matches the hostname in the host's own sources
  line and nothing the repo says. That is the one thing a repo cannot
  forge, but every upstream is served from the same host, so today it
  cannot tell them apart. It would if the front end served each upstream
  under a hostname of its own (a vhost rewriting
  ``zabbix.apt.example.com/ch/prod`` to ``/zabbix/ch/prod``).

What a pin does not protect against
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``o=`` and ``l=`` are fields the repo's owner writes in the Release. The
signature says who wrote it, not that it is true: a repo that began
claiming ``Origin: Ubuntu`` would step out of the deny stanza above. Set
``origin_pins:`` on the upstream in ``aptberg.yaml``::

    zabbix:
      origin_pins: [{origin: Zabbix, label: Zabbix}]

``fetch``, ``sync`` and a ``cut`` from scratch then refuse a Release whose
Origin and Label match none of the listed pairs, naming them all. (More
than one pair is for an upstream whose suites differ, like Debian and its
backports.) Nothing new reaches ``cur``, so nothing reaches ``acc`` or
``prod``. If upstream really renamed itself, review what changed, then
update ``origin_pins:`` here and the pref files on the hosts together.

Pinning only decides which versions are candidates. It is not a sandbox: a
package you do allow still runs its maintainer scripts as root. Beyond the
names it may touch, you rely on the ``acc`` to ``prod`` gate.

More
~~~~

- Put each repo's key in its own file and name it with ``Signed-By:`` in a
  deb822 ``.sources`` file (``/etc/apt/keyrings/zabbix.gpg``). That stops
  every other key from vouching for that repo. It does not stop the repo's
  own key from lying about its Origin, hence ``origin_pins:``.
- ``APT::Default-Release "noble";`` gives the ``noble*`` suites 990, so a
  third-party repo cannot win with a higher version of an Ubuntu package.
  It only matters for names both sides have.
- ``cuda-keyring`` installs
  ``/etc/apt/preferences.d/cuda-repository-pin-600``, which gives
  *everything* from ``l=NVIDIA CUDA`` priority 600, above Ubuntu. That is
  the reverse of an allow-list, and its ``origin *ubuntu.com*`` lines never
  match a host that gets Ubuntu from aptberg. The names are too many and
  too varied to allow-list, so edit that file (it is a conffile) into a
  deny-list: ``-1`` for the names that overlap with Ubuntu (``nvidia-*``,
  ``libnvidia-*``, ``nsight-*``) from ``o=NVIDIA``, then ``600`` for the
  rest.
- A deny-all also blocks what you do later need from that repo
  (``zabbix-release``, a helper library). When something is refused,
  ``apt-cache policy <package>`` shows which stanza decided.
- Test a change before rolling it out: ``apt-cache policy zabbix-agent
  libc6`` should show Zabbix winning the first and the second unchanged,
  and ``apt-get -s install zabbix-agent2`` shows what it would pull in.


Debian hosts on acc or prod say the Release file is expired. What now?
-----------------------------------------------------------------------

``apt update`` on a Debian host on ``acc`` or ``prod`` fails with::

    E: Release file for http://aptberg.example.com/debian-security/ch/prod/dists/trixie-security/InRelease
       is expired (invalid since 1d 4h 25min 13s). Updates for this
       repository will not be applied.

Debian's ``-security``, ``-updates`` and ``-backports`` suites carry a
``Valid-Until:`` of 7 days after their ``Date:``. Upstream refreshes it
every day; a frozen snapshot does not, so once a snapshot is more than 7
days old, apt refuses it. ``cur`` is never affected. The release suite
itself (``trixie``) has no ``Valid-Until``, and neither do Ubuntu's suites.

aptberg serves upstream's signed ``Release`` as is and cannot change the
field. The fix is on the hosts, for those three suites only. Pick one per
suite; the other suites stay on their channel either way.

Raise the limit: ``valid-until-min``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

apt can lengthen (never shorten) the validity per source, counted from the
Release's ``Date:``. 15 days::

    deb [valid-until-min=1296000] http://aptberg.example.com/debian/ch/prod trixie-updates main contrib non-free-firmware
    deb [valid-until-min=1296000] http://aptberg.example.com/debian/ch/prod trixie-backports main contrib non-free-firmware
    deb [valid-until-min=1296000] http://aptberg.example.com/debian-security/ch/prod trixie-security main contrib non-free-firmware

or ``Valid-Until-Min: 1296000`` in a deb822 ``.sources`` file. To set it
for every source at once (harmless for those without ``Valid-Until``)::

    # /etc/apt/apt.conf.d/99-dep-cooldown-raise-valid
    Acquire::Min-ValidTime "1296000";

- Choose at least the oldest a ``prod`` snapshot gets: the time from a cut
  to its promotion, plus the time until the next promotion, plus slack.
  Too short, and the error comes back between promotions.
- The suites keep their cooldown: ``prod`` still gets only what ``acc``
  had.
- The price is a longer replay window. A stale but validly signed Release
  is accepted for 15 days instead of 7, so a man in the middle can hold a
  host back on old security fixes that long. ``check-valid-until=no``
  works too, but removes the limit altogether; prefer ``valid-until-min``.

Take those suites from ``cur``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Point only the expiring suites at ``cur`` and leave the rest on ``prod``::

    deb http://aptberg.example.com/debian/ch/prod trixie main contrib non-free-firmware
    deb http://aptberg.example.com/debian/ch/cur trixie-updates main contrib non-free-firmware
    deb http://aptberg.example.com/debian/ch/cur trixie-backports main contrib non-free-firmware
    deb http://aptberg.example.com/debian-security/ch/cur trixie-security main contrib non-free-firmware

- No apt option and no replay trade-off: ``cur`` is as fresh as upstream.
- No cooldown for those suites: an ``acc`` and a ``prod`` host get the same
  updates at the same time, untested. For ``-security`` that is often
  what you want anyway; for ``-updates`` and ``-backports`` it gives up
  most of what the channels are for on Debian.

The two mix: ``-security`` from ``cur``, ``-updates`` and ``-backports``
on ``prod`` with ``valid-until-min``. Do not move the whole host to
``cur`` for this; only these suites expire.


How do I rush an urgent fix to prod without rushing everything else?
--------------------------------------------------------------------

Say ``ubuntu-security`` has a php fix that ``prod`` hosts need today.
Promoting ``ubuntu-security`` would bring every other package in that
suite along, without its soak. Instead, copy only the fixed ``.deb``
files into a PPA of your own and move that through ``cur``, ``acc``
and ``prod``. Here both are called ``emergency``: the PPA is a set of
aptly repositories ``emergency/<codename>`` on a PPA host, filled by
hand, and aptberg mirrors it as the upstream ``emergency``, which every
host already has in its sources. Copy the upstream files as they are,
not rebuilt: the same name, version and hash in two repositories is
one version to apt, so nothing needs pinning, and nothing conflicts
once the regular suite catches up.

1. Make sure ``cur`` has the fix::

       aptberg sync --apply debian-security ubuntu-security
       aptberg diff prod..cur debian-security ubuntu-security --source='php8*'

2. List the files ``cur`` has and ``prod`` does not, and read the list::

       aptberg diff prod..cur debian-security ubuntu-security \
           --source='php8*' --files > files.txt

   One line per file, after a ``#`` header: codename, component, size,
   sha256 and the path below the host. The sha256 comes from the
   upstream's signed index, so a file that matches it is the upstream's
   file. ``--source`` matches the source package, so
   ``libapache2-mod-php8.5`` and the ``universe`` binaries come along;
   every component and architecture is listed.

3. On the PPA host, in a directory with one subdirectory per codename,
   fetch each file, check it, and add it to that codename's aptly
   repository:

   .. code-block:: sh

       baseurl=http://aptberg.example.com
       aptly_prefix=emergency/

       success=true
       while read codename component size hash debpath; do
           case $codename in '#'*) continue;; esac
           lofile="$codename/${debpath##*/}"
           test -d "$codename" || {
               echo "missing $codename dir" >&2; success=false; break; }
           test -f "$lofile" ||
               wget -qO "$lofile" "$baseurl/$debpath" || {
                   rm -f "$lofile"; success=false; break; }
           lohash=$(openssl sha256 -hex "$lofile" | sed -e 's/.*= //')
           if test "$hash" != "$lohash"; then
               echo "hash mismatch: $lofile $hash $lohash" >&2
               success=false; break
           fi
           aptly repo add "${aptly_prefix}${codename}" "$lofile" || {
               success=false; break; }
       done < files.txt

       $success && echo OK || echo FAILED
       $success

   Then publish the updated repositories as usual (``aptly snapshot create`` +
   ``aptly publish snapshot``).

4. Mirror the PPA into ``cur`` as the ``emergency`` upstream, and check
   that it carries the files::

       aptberg sync --apply emergency
       aptberg diff acc..cur emergency

5. If your hosts are configured to use the ``cur`` branch of the emergency
   mirror, you're done. If it's set to ``acc`` or ``prod``, then promote both::

       aptberg promote --stage=cur emergency --apply
       aptberg promote --stage=acc emergency --apply

   Commit the changed manifests, as after any promotion.

6. Remove the copies again once the regular suite has caught up: when
   ``aptberg diff prod..cur debian-security ubuntu-security --source='php8*'``
   no longer shows the versions you copied, ``prod`` serves them itself. Remove
   them from the PPA, publish, and take it through steps 4 and 5 again.  Until
   then, nothing breaks: hosts see one version in two places.
