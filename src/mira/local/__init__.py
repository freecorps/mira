"""The local review surface: Mira run from a developer's own checkout.

Phase 7A. One command — ``mira local review`` — reviews a diff that never
leaves the machine's git repository: the working tree, the index, or a commit
range. It is the *same* review as the one the server runs on a pull request,
and that is the whole point of the phase. The engine, the configuration
loader, the retrieval of learned rules and indexed context, the pre-merge check
framework and the rendered output are all imported from where the server gets
them. Nothing about a finding is re-implemented here.

What this package adds is everything that is genuinely local:

* turning "the working tree", "what is staged" or ``main..HEAD`` into a
  unified diff, using read-only git plumbing;
* deciding which repository the change belongs to, so retrieval and policy
  resolution scope to the same repository the server would have used;
* refusing to send the code anywhere the repository did not ask for it;
* exit codes a CI job can branch on, and a JSON shape it can parse.

Three properties are load-bearing and each is tested.

**Read-only.** No git subcommand outside a reviewed allowlist is ever invoked,
every invocation carries ``--no-optional-locks``, no platform provider is
constructed, and nothing is written to Mira's own store — a local review leaves
no check run, no review row and no finding behind. A developer running this in
a loop must not be able to teach the deployment anything.

**One destination.** The code in the diff is sent to the model the repository's
configuration names, and to nothing else. A ``--model`` flag, a ``MIRA_MODEL``
environment variable or a dashboard override that would redirect it to a
different vendor or endpoint is refused rather than honoured. See
:mod:`mira.local.guard`.

**Offline is a degradation, not a crash.** Nothing here needs the forge. A
repository with no index, no network and no credentials still produces a
review, with the parts that could not run named in the output rather than
silently absent.
"""

from __future__ import annotations

from mira.local.exit_codes import ExitCode

__all__ = ["ExitCode"]
