"""An empty `?date=` must mean the same thing as no `?date=` at all.

`params.get('date', <today>)` only defaults when the key is **absent** — but a browser
sends a cleared control as `?date=` (present, empty). Every route that defaults a date
took that empty string as a literal date value and matched nothing: the pipeline page
rendered a bare graph while the run it was supposed to show sat right there.

Checked per route rather than per line, so a new route that reinvents the trap fails
here instead of on someone's screen.
"""
import ast
import pathlib

import pytest


CONSOLE_API = pathlib.Path(__file__).resolve().parent.parent
_TREES = [CONSOLE_API / 'routes', CONSOLE_API / 'ee']


def _modules():
    return sorted(p for tree in _TREES if tree.exists()
                  for p in tree.rglob('*.py')
                  if p.name != '__init__.py' and 'tests' not in p.parts)


def _defaulted_gets(tree):
    """`params.get('date', <default>)` — the two-arg form, which is the trap.

    Scoped to `params` on purpose: `item.get('date', date)` on a DynamoDB row is a
    different thing entirely, and defaulting *that* is fine.
    """
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'get'
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'params'
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'date'):
            continue
        default = ast.unparse(node.args[1])
        if default not in ("''", '""'):            # `params.get('date', '') or X` is safe
            out.append(ast.unparse(node))
    return out


@pytest.mark.parametrize('path', _modules(), ids=lambda p: p.name)
def test_no_route_defaults_a_date_with_the_two_arg_get(path):
    offenders = _defaulted_gets(ast.parse(path.read_text()))
    assert not offenders, (
        f"{path.name} defaults `date` via the two-arg .get(), which only fires when the "
        f"param is absent — a cleared picker sends it present and empty, and the default "
        f"never runs: {offenders}. Use `params.get('date') or <default>`. See ADR #106."
    )


def test_the_rule_has_teeth():
    """A guard that cannot fail is not a guard."""
    trap = ast.parse("date = params.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))")
    assert _defaulted_gets(trap)

    safe = ast.parse("date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')")
    assert not _defaulted_gets(safe)

    also_safe = ast.parse("date = params.get('date', '') or 'x'")
    assert not _defaulted_gets(also_safe)


def test_the_walk_actually_reaches_the_routes():
    names = {p.name for p in _modules()}
    assert {'pipelines_list.py', 'pipelines_info.py', 'tasks.py'} <= names


# ──────────────────────────────────────────────────────────────────────────────
# The behaviour, not just the shape: an empty ?date= must behave as no ?date=.
# ──────────────────────────────────────────────────────────────────────────────

def _status_event(date=None):
    q = {'name': 'p'}
    if date is not None:
        q['date'] = date
    return {'queryStringParameters': q}


def _rows(today):
    return [{'execution_name': 'extract-1', 'task_name': 'extract', 'pipeline_name': 'p',
             'pipeline_execution': f'p-{today}-a', 'status': 'success', 'date': today,
             'started_at': f'{today}T09:00:00.000Z'}]


class TestEmptyDateBehavesAsAbsent:
    """Reported symptom: clear the date on History, open the pipeline — bare graph,
    no banner, while the run sits right there. The route read '' as a literal date."""

    def _today(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

    def test_pipeline_status_falls_back_to_today(self, mocker):
        from routes import pipelines_list
        today = self._today()
        scan = mocker.patch.object(pipelines_list.executions_repo, 'scan',
                                   return_value=_rows(today))
        mocker.patch.object(pipelines_list.pipelines_repo, 'get', return_value=None)

        pipelines_list.get_pipeline_status('p', _status_event(date=''))

        expr = scan.call_args.kwargs['FilterExpression'].get_expression()
        dates = [v.get_expression()['values'][1] for v in expr['values']
                 if hasattr(v, 'get_expression') and v.get_expression()['operator'] == '=']
        assert today in dates, f'empty ?date= must query today, queried {dates}'
        assert '' not in dates

    def test_empty_and_absent_give_the_same_answer(self, mocker):
        import json
        from routes import pipelines_list
        today = self._today()
        mocker.patch.object(pipelines_list.executions_repo, 'scan', return_value=_rows(today))
        mocker.patch.object(pipelines_list.pipelines_repo, 'get', return_value=None)

        empty = json.loads(pipelines_list.get_pipeline_status('p', _status_event(date=''))['body'])
        absent = json.loads(pipelines_list.get_pipeline_status('p', _status_event())['body'])

        assert len(empty['tasks']) == len(absent['tasks']) == 1

    def test_pipeline_dag_falls_back_to_today(self, mocker):
        from routes import pipelines_info
        today = self._today()
        scan = mocker.patch.object(pipelines_info.executions_repo, 'scan',
                                   return_value=_rows(today))
        mocker.patch.object(pipelines_info.pipelines_repo, 'get', return_value=None)

        pipelines_info.get_pipeline_dag('p', {'queryStringParameters': {'name': 'p', 'date': ''}})

        expr = scan.call_args.kwargs['FilterExpression'].get_expression()
        dates = [v.get_expression()['values'][1] for v in expr['values']
                 if hasattr(v, 'get_expression') and v.get_expression()['operator'] == '=']
        assert today in dates and '' not in dates

    def test_an_explicit_date_still_wins(self, mocker):
        from routes import pipelines_list
        scan = mocker.patch.object(pipelines_list.executions_repo, 'scan', return_value=[])
        mocker.patch.object(pipelines_list.pipelines_repo, 'get', return_value=None)

        pipelines_list.get_pipeline_status('p', _status_event(date='2024-01-15'))

        expr = scan.call_args.kwargs['FilterExpression'].get_expression()
        dates = [v.get_expression()['values'][1] for v in expr['values']
                 if hasattr(v, 'get_expression') and v.get_expression()['operator'] == '=']
        assert '2024-01-15' in dates
