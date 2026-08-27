"""Phase 5 — assisted, safe correction.

Turning a finding into a change is the first thing Mira does that *writes*. The
whole module is arranged around keeping that power narrow: generation, patch
application, validation and publication are four separate steps with four
separate failure modes, and none of them can reach the default branch.

Nothing here runs unless a deployment turns it on. ``autofix.mode`` is ``off``
by default, and off is also what a kill switch, an unknown repository and an
unreadable permission all resolve to.
"""
