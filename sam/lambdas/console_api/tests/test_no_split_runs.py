"""One rule, enforced across every surface that derives a run's status (ADR #113):

    a run's status is derived from the task rows the reader got, so a reader that
    cuts inside a pipeline does not lose a run — it invents one.

``date-pipeline-index`` is ordered by ``pipeline_name``, so a row-count cut lands
mid-pipeline. ``ExecutionsRepo.query_by_date`` cuts that way; ``query_runs_by_date``
cuts on a pipeline boundary. Anything that aggregates must use the latter.

This exists because the /runs fix was found by hand, and the same bug was then sitting
in three more places nobody had thought to look at (the sidebar card, the calendar, the
history dropdown). A rule that has to be remembered is a rule that gets missed — so it
is checked here instead, by walking the module rather than trusting a grep to have been
run. A new aggregating surface on the capped read fails this test on arrival.
"""

import ast
import pathlib

import pytest


ROUTES = pathlib.Path(__file__).resolve().parent.parent
_TREES = [ROUTES / 'routes', ROUTES / 'ee']      # `ee/` exists only in the merged EE build

# The canonical derivation and the helper that wraps it (ADR #112). A module calling
# either is turning task rows into runs.
_DERIVES = {'derive_execution_status', '_aggregate_executions'}

# Reads that may hand back half a pipeline.
_SPLITS = {'query_by_date'}


def _calls(tree):
    """Every function name called anywhere in the module."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _route_modules():
    return sorted(p for tree in _TREES if tree.exists()
                  for p in tree.rglob('*.py')
                  if p.name != '__init__.py' and 'tests' not in p.parts)


@pytest.mark.parametrize('path', _route_modules(), ids=lambda p: p.name)
def test_a_module_that_derives_run_status_never_uses_the_splitting_read(path):
    tree = ast.parse(path.read_text())
    called = _calls(tree)

    if not (called & _DERIVES):
        return          # not an aggregating surface; the capped read is fine here

    offenders = called & _SPLITS
    assert not offenders, (
        f"{path.name} derives a run's status but reads with {sorted(offenders)}, which "
        f"cuts on a row count and so can hand it half of a run's tasks — the run then "
        f"renders a wrong status, not a missing one. Use query_runs_by_date "
        f"(whole pipelines) instead. See ADR #113."
    )


def test_the_rule_has_teeth():
    """A guard that cannot fail is not a guard: prove the check actually fires."""
    tree = ast.parse(
        "from dal import executions_repo\n"
        "from constants import derive_execution_status\n"
        "def surface():\n"
        "    rows = executions_repo.query_by_date('2026-07-16', max_items=500)\n"
        "    return derive_execution_status({r['status'] for r in rows})\n"
    )
    called = _calls(tree)
    assert called & _DERIVES
    assert called & _SPLITS


def test_at_least_one_module_is_actually_covered():
    """And that the walk finds the surfaces, rather than quietly matching nothing."""
    aggregating = [p.name for p in _route_modules()
                   if _calls(ast.parse(p.read_text())) & _DERIVES]
    assert {'executions.py', 'pipelines_list.py'} <= set(aggregating)


# ──────────────────────────────────────────────────────────────────────────────
# Same date sequence, one source of truth (Principle #1). The sidebar's 14-day
# rollup and the History fan-out used to compute the SLA window independently —
# byte-identical output, two implementations. Consolidated onto feed.feed_dates.
#
# Checked by parsing the source, not by mocking `feed_dates` and asserting it was
# called: that would test "list_pipelines called this function", not "the shared
# window is really the one in use" — the reinvented duplicate this guards against
# would ALSO satisfy a call-count assertion if someone left both in place. The
# real risk is drift between two implementations of the same sequence, which a
# structural check catches and a call-mock does not (Principle #14 — mocking an
# internal helper tests the mock, not the system).
# ──────────────────────────────────────────────────────────────────────────────

def test_list_pipelines_queries_exactly_the_dates_feed_dates_produces(mocker):
    """The behavioural half: freeze the clock, let both `list_pipelines` and
    `feed.feed_dates('')` run for real, and check the DDB reads landed on the exact
    same dates. Mocks only the DDB boundary (`query_runs_by_date`) and the registry
    listing — never `feed_dates` itself, so this fails if the two ever drift instead
    of only if the call disappears.
    """
    from datetime import datetime, timezone
    import feed
    from routes import pipelines_list

    frozen = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
    mocker.patch.object(feed, 'datetime', mocker.Mock(now=lambda tz=None: frozen))
    mocker.patch.object(pipelines_list.pipelines_repo, 'list_all',
                        return_value=[{'pipeline_name': 'sales', 'sfn_arn': 'arn:x'}])
    mocker.patch.object(pipelines_list.executions_repo, 'scan_raw', return_value={'Items': []})
    queried = mocker.patch.object(pipelines_list.executions_repo,
                                  'query_runs_by_date', return_value=[])

    pipelines_list.list_pipelines({'queryStringParameters': {'stats': 'true'}})

    expected = feed.feed_dates('')          # the real function, same frozen clock
    assert [c.args[0] for c in queried.call_args_list] == expected


def test_list_pipelines_uses_the_shared_window_not_a_reinvented_one():
    tree = ast.parse((ROUTES / 'routes' / 'pipelines_list.py').read_text())
    calls = _calls(tree)
    assert 'feed_dates' in calls, (
        "list_pipelines's SLA-window rollup must call feed.feed_dates(''), the same "
        "sequence every History feed walks — not reimplement the date arithmetic."
    )

    # The reinvented shape this replaced: a list-comp deriving dates via `timedelta`
    # over `range(...SLA_DAYS...)`, right where the shared call now sits.
    for node in ast.walk(tree):
        if isinstance(node, ast.ListComp) and 'timedelta' in ast.unparse(node):
            raise AssertionError(
                f"found an inline date-walk list comprehension in pipelines_list.py "
                f"({ast.unparse(node)[:80]}…) — this is the duplicate feed_dates('') "
                f"replaced; a second implementation drifts from the shared one silently."
            )
