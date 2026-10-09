aptberg TODO
============

Open questions about aptberg's interface and behaviour: decided neither
way yet, and worth an issue each once the repository is public.


One name for "show the whole list"
----------------------------------

Several commands cut a long list short, and each has its own option to
show all of it:

- ``fetch``, ``sync`` and ``backfill``: ``--show-missing`` lists every
  unsatisfied dependency, not just the first 50;
- ``verify``: ``--verbose-errors`` lists every error, not just the first
  ten.

One option name for both would be easier to remember. ``gc --list`` is
different (it prints every key to be deleted) and stays as it is.


retire's ``--grace`` and ``--older-than``
-----------------------------------------

``retire`` takes two ages, and they are easy to mix up:

- ``--grace`` (7d): keep snapshots a prefix served this recently. It must
  not be shorter than ``gc``'s ``--grace``, or ``gc`` fails on a snapshot
  it still needs; for now the help texts of both say so.
- ``--older-than`` (30d), with ``--unused``: keep snapshots cut more
  recently.

Open: clearer names, or one age deriving the other, or a check that
refuses a retire grace shorter than gc's. Moving them into
``aptberg.yaml`` was considered and rejected.


Keeping a snapshot on purpose
-----------------------------

``retire --unused`` drops everything its policy allows. There is no way
to keep a particular snapshot longer -- a release-day baseline, say --
other than leaving a manifest pointing at it. Whether that is enough, or
snapshots need a "keep" mark that retire honours, is open.
