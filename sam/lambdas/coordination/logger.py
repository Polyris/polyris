"""
Structured JSON Logger for Console API

Outputs JSON lines to stdout (CloudWatch picks up automatically).
Enables CloudWatch Insights queries like:
    filter level = "ERROR"
    filter fn = "skip_task" and task = "etl_load"
    stats count(*) by fn

Usage:
    from logger import log
    
    log.info("skip_task", "Successfully skipped", task="etl_load", execution="etl_load-2026-01-15-abc123")
    log.warn("skip_task", "Failed to notify dependents", execution="exec-123")
    log.error("skip_task", "Unexpected error", error=str(e))
"""

import json


class _Logger:
    """Minimal structured logger. Prints JSON to stdout for CloudWatch."""

    def _emit(self, level: str, fn: str, msg: str, **kwargs):
        entry = {"level": level, "fn": fn, "msg": msg}
        if kwargs:
            entry.update(kwargs)
        print(json.dumps(entry, default=str))

    def info(self, fn: str, msg: str, **kwargs):
        self._emit("INFO", fn, msg, **kwargs)

    def warn(self, fn: str, msg: str, **kwargs):
        self._emit("WARN", fn, msg, **kwargs)

    def error(self, fn: str, msg: str, **kwargs):
        self._emit("ERROR", fn, msg, **kwargs)


log = _Logger()
