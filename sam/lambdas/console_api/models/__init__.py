"""Domain models for the Console API.

Typed value-objects over persisted records. Separated from the DAL
(persistence) and routes (HTTP) layers so domain semantics live in one
place. See ADR #83.
"""
from models.backfill_record import BackfillRecord

__all__ = ['BackfillRecord']
