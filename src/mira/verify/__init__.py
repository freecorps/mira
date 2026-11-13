"""Execution-backed review: run the pull request's code, and believe what it does.

Every finding Mira posted used to rest on a model's reading of the diff, and the
only check on that reading was another model's opinion (``self_critique``). This
package adds the one kind of evidence neither model can argue with: what the
code does when it runs.

Two things happen in a sandboxed workspace holding the pull request's head:

* **Tool evidence** (:mod:`mira.verify.evidence`). Configured linters, type
  checkers and the tests related to the change run once; their diagnostics on
  the changed lines go into every review part's prompt, and — when the
  pre-merge checks do not already post that tool — become findings themselves.
* **Finding verification** (:mod:`mira.verify.reproduce`). After the critic, the
  strongest logic findings each get a minimal reproduction test written by the
  review model and run in the workspace. A test that fails the way the finding
  said it would marks the finding *reproduced*; one that passes downgrades it;
  anything else changes nothing.

The workspace and its isolation are :mod:`mira.verify.workspace` and
:mod:`mira.verify.isolation`; :mod:`mira.verify.session` ties one review's use
of them together, so the review engine touches this package in four places.

Kept import-light: :mod:`mira.config` imports the config models from here.
"""
