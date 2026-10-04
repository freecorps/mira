"""License data and license policy for dependencies. No model is ever called.

* :mod:`~mira.licenses.expressions` — SPDX ids and expressions; normalizing
  what registries write (``MIT License``, ``Apache 2.0``) into one spelling.
* :mod:`~mira.licenses.lookup` — a package's license from its lockfile, the
  cache, or its registry over the guarded release-notes HTTP client.
* :mod:`~mira.licenses.policy` — ``licenses.allow`` / ``deny`` evaluation.
* :mod:`~mira.licenses.review` — the review-time check on added dependencies.

See docs/sbom-licenses.md.
"""

from mira.licenses.expressions import normalize
from mira.licenses.policy import Policy, Verdict, evaluate

__all__ = ["Policy", "Verdict", "evaluate", "normalize"]
