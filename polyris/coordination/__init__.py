"""Pure coordination logic (ADR #130, ADR #137).

Decides each row of a run from facts: dependency statuses, asset facts, the
run's pauses and its stop. Nothing here calls AWS; the coordination Lambdas
load the facts and write what was decided.

The coordination Lambda and console_api import this package from the polyris
release their ``requirements.txt`` pins (ADR #102, ADR #130 §10), so every
runtime evaluates readiness with the same code from the same release. Modules
reach outside the package only through ``..partitions`` and ``..constants``.
"""
