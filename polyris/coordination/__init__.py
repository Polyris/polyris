"""Pure coordination logic (ADR #130).

Decides whether a waiting task is ready from facts: dependency statuses, asset
facts and the pause flag. Nothing here calls AWS; the reconciler Lambda loads
the facts and acts on the decision.

The coordination Lambda and console_api import this package from the polyris
release their ``requirements.txt`` pins (ADR #102, ADR #130 §10), so every
runtime evaluates readiness with the same code from the same release. Modules
reach outside the package only through ``..partitions`` and ``..constants``.
"""
