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
backports.) Nothing reaches ``acc``, so nothing reaches ``prod``. If
upstream really renamed itself, review what changed, then update
``origin_pins:`` here and the pref files on the hosts together.

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
