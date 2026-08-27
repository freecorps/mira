"""Checks that read something the diff does not contain.

A ticket tracker and a CI run are both outside the pull request, and both bring
the same failure mode with them: an answer that means "no" and an answer that
means "I could not ask" arrive over the same wire and look alike. Every module
here is written around telling those apart, because reporting the second as the
first turns an outage into a wave of violations across every open pull request
in the install.

No external service is required. The default ticket adapter asks the hosting
platform Mira is already authenticated against, ``provider: "none"`` disables
lookups entirely, and CI is read through the same provider methods the merge
gate already uses.
"""
