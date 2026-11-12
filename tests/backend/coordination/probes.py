"""Mutation probes: each re-introduces one defect in the new code (ADR #130, Testing).

A probe is (defect id, file, old text, new text, scenario node id). The
runner (`python tests/backend/coordination/run_probes.py`) applies each
probe, runs its scenario, expects it to fail, and restores the file.
Probes are added as their scenarios are bound; test_meta.py requires one for
every defect whose scenario runs.
"""

from typing import NamedTuple, Tuple


class Probe(NamedTuple):
    defect: str
    path: str          # repo-relative file the probe mutates
    old: str           # exact text, which must occur once
    new: str
    scenario: str      # pytest node id that must fail under the probe


PROBES: Tuple[Probe, ...] = ()
