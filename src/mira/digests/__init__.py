"""What landed on the default branch: per-area digests and release notes.

Three pieces share one pipeline:

* :mod:`~mira.digests.collect` asks the provider what was merged (and pushed
  straight to the branch) in a window, or between two refs;
* :mod:`~mira.digests.areas` sorts those changes into areas by path;
* :mod:`~mira.digests.summarize` and :mod:`~mira.digests.release_notes` turn
  them into prose with one bounded model call each, quoting every pull request
  title and body as untrusted data.

:mod:`~mira.digests.service` builds, stores and delivers a digest, and
:mod:`~mira.digests.runtime` runs it on the configured schedule inside
``mira serve``. Everything here reads; nothing writes to a repository.
"""
