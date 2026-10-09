aptberg findings
================

Real-world facts about upstream archives that shaped a design decision,
found by checking the live archives rather than assumed. Add to this file
when a choice in the code exists *because* of a specific upstream quirk --
so the next person hitting the same surprise does not have to re-derive it.


Debian and Ubuntu do not share pool bytes, ever (2026-09-29)
------------------------------------------------------------

A pool in ``_pool/`` is deduplicated by ``Filename:`` (the archive-relative
pool path): within it, that path must name one immutable blob of bytes.
That holds *within* one archive -- Debian and Ubuntu each guarantee it
themselves -- but does not hold *across* them, and the gap is not a rare
edge case.

Checked ``deb.debian.org/debian`` trixie against
``nl.archive.ubuntu.com/ubuntu`` noble, main component, amd64 (plus
arch:all): 326 filenames exist in both archives at the identical
``name_version_arch``. All 326 have different ``SHA256:``. Zero are
byte-identical.

Example, ``python3-jmespath_1.0.1-1_all.deb``::

    debian: Size=21112 SHA256=2b4351db7f00a8e4840140572b337a5005f897eaf6c7d9c929991e85a152d388
    ubuntu: Size=21328 SHA256=d5993df32fe9a15bb7832c9e25fff3a3e641362a8905714d0a6705e122839b52

Cause: Ubuntu's sync tooling rewrites ``Maintainer:`` to "Ubuntu
Developers" and adds ``Original-Maintainer:`` on every package it pulls in
from Debian, recompressing ``changelog.Debian.gz`` in the process -- even
when nothing else about the package changed. It does this *without*
bumping the version string (no ``buildN``/``ubuntuN`` suffix), because
Ubuntu's archive tooling does not consider a maintainer-field rewrite a
package change worth a new version. The ``hunspell-dict-ko`` collision that
triggered this check (seen in 2019) is not a fluke from that year: it is
what happens *every time* a same-version package exists in both archives.

Consequences for design:

- Never assume a ``Filename:`` is a safe global dedup key across upstreams
  from different organizations. It is safe within one upstream's
  ``group:`` (aptberg's own ``unit``), because that is the boundary one
  archive's own release process actually guarantees; it is not safe beyond
  it.
- There is no free disk-space lunch to give up by partitioning the pool
  per archive: the identical-content overlap this would sacrifice is
  approximately zero between Debian and Ubuntu today. Splitting the pool by
  ``unit`` costs nothing measurable, at least for these two.
- This is Ubuntu-side behaviour aptberg has no visibility into or control
  over, and it is not fixable by excluding the one 2019 incident: a new
  instance can appear for any package, at any time, without warning. The
  fix has to be structural (a pool per unit, and ``pool:`` only where
  sharing is promised), not a growing ``hard_exclude`` list of known
  offenders.
