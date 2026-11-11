"""Asset-layer wake-up catches must log at ERROR, not WARN.

Pins the log-level upgrade from commit 4e7253d. These catches cover paths
that, when they fail, silently leave tasks hanging (`wait_for` never resumes)
or fire consumers on bad data (freshness parse fail-open). Downgrading any of
them to `log.warn` masks exactly the class of coordination bug (1.19, 1.20)
that this branch is working around until the real fix lands.

Per `polyris/CLAUDE.md`: catches on a wake-up path log at `error`, never
`warn` — warn is for state-sync best-effort, not for anything a pull-waiter
depends on.
"""

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.parent
CHECK_ASSETS = REPO_ROOT / "sam" / "lambdas" / "check_assets" / "index.py"
NOTIFY_SUBS = REPO_ROOT / "sam" / "lambdas" / "notify_asset_subscribers" / "index.py"


# Each tuple: (file, handler-string-as-logged, description).
# All six catches below MUST appear as `log.error("<handler>", ...)` inside a
# generic `except Exception` block — WARN at any of them silently masks the
# coordination bugs this branch flags as experimental limitations.
_WAKE_UP_CATCHES = [
    (CHECK_ASSETS, "_save_subscription",
     "wait_for subscription write; failure means task hangs to timeout"),
    (CHECK_ASSETS, "_is_fresh",
     "freshness timestamp parse on check_assets side (fail-closed)"),
    (NOTIFY_SUBS, "_get_asset_subscribers",
     "subscriber query; failure means pull-waiters are not woken"),
    (NOTIFY_SUBS, "_check_freshness",
     "freshness timestamp parse on notify side (fail-open — doubly important to see)"),
    (NOTIFY_SUBS, "_coordinate_ready_check",
     "evaluate_deps coordination invoke (fail-open to signal anyway)"),
    (NOTIFY_SUBS, "_send_task_success",
     "generic Exception branch — IAM / outage / malformed output"),
]


def _except_block_log_levels(src: str, handler: str) -> list[str]:
    """Return log levels used in `log.<level>("<handler>", ...)` calls that
    appear inside a generic `except Exception` block (next-except or next-
    function-def closes the block)."""
    levels: list[str] = []
    for match in re.finditer(r'except\s+Exception\b[^\n]*\n', src):
        start = match.end()
        # Scope: until the next `except`, next `def`, or end of file. Comment
        # lines between `except` and the log call don't bound the block, but a
        # new except or function header does.
        next_boundary = min(
            (idx for idx in [
                src.find('\n    except ', start),
                src.find('\ndef ', start),
                src.find('\n\ndef ', start),
            ] if idx != -1),
            default=len(src),
        )
        window = src[start:next_boundary]
        lvl_match = re.search(
            rf'log\.(\w+)\(\s*"{re.escape(handler)}"',
            window,
        )
        if lvl_match:
            levels.append(lvl_match.group(1))
    return levels


@pytest.mark.parametrize("path, handler, description", _WAKE_UP_CATCHES)
def test_wake_up_catch_logs_at_error(path, handler, description):
    src = path.read_text()
    levels = _except_block_log_levels(src, handler)
    assert levels, (
        f"no `except Exception` → log.*({handler!r}, ...) pair found in "
        f"{path.name}; the catch may have been removed entirely"
    )
    bad = [lvl for lvl in levels if lvl != "error"]
    assert not bad, (
        f"{handler} in {path.name} has a non-error log level in an "
        f"`except Exception` block: {bad}. This masks a wake-up-path failure "
        f"({description}). See polyris/CLAUDE.md wake-up-path log rule."
    )
