"""
polyris.codegen — generators for derived artifacts from canonical
sources (polyris/constants.py, polyris/schema.py, etc.).

v0.79.0 (ADR #72): adds `sync_enums` — propagates enum families from
polyris/constants.py to:
  - sam/lambdas/_shared/constants.py (and per-Lambda copies via sync-constants)
  - sam/lambdas/console_api/constants.py
  - ui/src/generated/enums.ts

Idempotent and CI-driftable: re-running with no source changes produces
identical output bytes. CI fails if a developer edits Python constants
without regenerating.
"""
