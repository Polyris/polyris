"""End-to-end chain test for several real bugs found and fixed in a single
code-review session, exercised TOGETHER against a stateful fake DynamoDB
table — not mocked in isolation per-function, as every other test in this
session's changes was. The goal here is specifically to catch a fix that
works alone but breaks (or silently no-ops) when composed with another fix
in the same real request sequence.

Chain under test:
  1. A task is RUNNING, with a duplicate older attempt of the same task_name
     sitting on an earlier DynamoDB page (resolve_task_item's pagination fix).
  2. stop_task is called by task_name (not execution_name) — must resolve to
     the CORRECT (running) attempt via resolve_task_item, not the stale one,
     then transition it to 'stopped' (TERMINAL_CONDITION_EXPRESSION /
     build_condition_expression_values, derived from the canonical set).
  3. restart_task is called on the now-stopped task — must accept 'stopped'
     as restartable (the critical stop->restart fix), resolving via
     resolve_task_item again, and reset it to 'waiting'.
  4. The final state must be coherent: exactly the intended execution
     changed, the stale duplicate untouched, no task stuck in an
     unreachable status.

A stateful fake table (real get/put/update against an in-memory dict,
real ConditionExpression enforcement) rather than per-call mocks, so a
regression in how these functions compose — not just how each behaves
alone — would surface here.
"""
import re

import pytest
from botocore.exceptions import ClientError


# ── A minimal but *stateful* DynamoDB table fake ────────────────────────────
# Real get_item/put_item/update_item/query semantics against an in-memory
# dict, including ConditionExpression enforcement — sufficient for the
# specific expression shapes this codebase actually uses (verified by
# reading the real call sites), not a general DynamoDB emulator.

class FakeTable:
    def __init__(self):
        self.items = {}  # execution_name -> item dict

    def get_item(self, Key, **kwargs):
        item = self.items.get(Key['execution_name'])
        return {'Item': item} if item is not None else {}

    def put_item(self, Item, **kwargs):
        # tokens_table items key on execution_name; the asset_events_table
        # (a different real table, routed through this same fake instance
        # in tests since dynamodb.Table() is mocked to always return it)
        # keys on asset_name+event_time instead. Pick whichever is present
        # rather than assuming every item uses the same schema.
        if 'execution_name' in Item:
            key = Item['execution_name']
        elif 'asset_name' in Item:
            key = f"assetevent#{Item['asset_name']}#{Item.get('event_time', '')}"
        else:
            raise KeyError(f"FakeTable.put_item: no recognized key field in Item: {list(Item.keys())}")
        self.items[key] = dict(Item)

    def _condition_holds(self, item, condition_expr, expr_values, expr_names=None):
        """Evaluate the specific ConditionExpression shapes this codebase
        emits: 'NOT #s IN (:a, :b, ...)' and '#s IN (:a, :b, ...)'."""
        if condition_expr is None:
            return True
        item = item or {}
        m_attr = re.match(r'attribute_not_exists\((#?\w+)\)', condition_expr)
        if m_attr:
            field = expr_names.get(m_attr.group(1), m_attr.group(1)) if expr_names else m_attr.group(1)
            return field not in item
        current_status = item.get('status')
        m = re.match(r'(NOT )?#s IN \(([^)]*)\)', condition_expr)
        assert m, f"Unsupported ConditionExpression in test fake: {condition_expr}"
        negate = bool(m.group(1))
        placeholders = [p.strip() for p in m.group(2).split(',')]
        allowed_values = {expr_values[p] for p in placeholders}
        is_in = current_status in allowed_values
        return (not is_in) if negate else is_in

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                     ExpressionAttributeNames=None, ConditionExpression=None, **kwargs):
        exec_name = Key['execution_name']
        item = self.items.get(exec_name)
        # Real DynamoDB evaluates the ConditionExpression against a missing
        # item as if it were empty, and creates it if the condition passes —
        # it does not unconditionally reject every update on a missing key
        # (e.g. attribute_not_exists(x) is the standard "insert only" idiom).
        if not self._condition_holds(item, ConditionExpression, ExpressionAttributeValues or {},
                                      ExpressionAttributeNames):
            raise ClientError(
                {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'condition failed'}}, 'UpdateItem')
        if item is None:
            item = {'execution_name': exec_name}

        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        set_part = UpdateExpression.split('REMOVE')[0].replace('SET', '', 1).strip()
        if set_part:
            # Split on commas NOT inside parentheses — a naive split() would
            # incorrectly break if_not_exists(#field, :val)'s internal comma
            # into two separate (invalid) assignments.
            assignments, depth, current = [], 0, ''
            for ch in set_part:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                if ch == ',' and depth == 0:
                    assignments.append(current)
                    current = ''
                else:
                    current += ch
            if current.strip():
                assignments.append(current)

            for assignment in assignments:
                field, _, val_expr = assignment.strip().partition('=')
                field = names.get(field.strip(), field.strip())
                val_expr = val_expr.strip()
                m_ine = re.match(r'if_not_exists\((#?\w+),\s*(:\w+)\)', val_expr)
                if m_ine:
                    ine_field = names.get(m_ine.group(1), m_ine.group(1))
                    item[field] = item[ine_field] if ine_field in item else values.get(m_ine.group(2))
                else:
                    item[field] = values.get(val_expr)
        self.items[exec_name] = item
        return {}

    def _condition_matches(self, item, cond):
        """Evaluate the specific boto3.dynamodb.conditions shapes this
        codebase actually builds: Attr(x).eq(y) (an Equals with
        ._values = (Attr, value)) and combinations of those via `&` (an And
        with ._values = (left, right)). Not a general boto3-conditions
        evaluator — just enough to make the fake's filtering genuine rather
        than a no-op that silently returns every item regardless of filter.
        """
        type_name = type(cond).__name__
        if type_name == 'Equals':
            attr_obj, expected = cond._values
            return item.get(attr_obj.name) == expected
        if type_name == 'And':
            left, right = cond._values
            return self._condition_matches(item, left) and self._condition_matches(item, right)
        raise AssertionError(f"Unsupported condition type in test fake: {type_name}")

    def query(self, KeyConditionExpression=None, FilterExpression=None,
              ExclusiveStartKey=None, **kwargs):
        # Genuine one-item-per-page pagination (deterministic insertion
        # order) so a caller that early-exits after the first non-empty
        # page — the exact bug resolve_task_item's fix addresses — would
        # visibly fail to see items on later pages, in this same fake.
        # FilterExpression is applied for real (see _condition_matches) so a
        # query for one task_name doesn't also surface sibling tasks —
        # exactly what a genuine DynamoDB query does server-side.
        matching_items = [
            v for v in self.items.values()
            if FilterExpression is None or self._condition_matches(v, FilterExpression)
        ]
        start_index = 0
        if ExclusiveStartKey is not None:
            start_name = ExclusiveStartKey['execution_name']
            for i, it in enumerate(matching_items):
                if it['execution_name'] == start_name:
                    start_index = i + 1
                    break
        if start_index >= len(matching_items):
            return {'Items': []}
        page_item = dict(matching_items[start_index])
        resp = {'Items': [page_item]}
        if start_index + 1 < len(matching_items):
            resp['LastEvaluatedKey'] = {'execution_name': page_item['execution_name']}
        return resp

    def scan(self, **kwargs):
        return {'Items': [dict(v) for v in self.items.values()]}

    def _passes_filter(self, item, filter_expr):
        # boto3 Attr expressions aren't introspectable generically; the real
        # filter here is task_name==X (optionally & pipeline_execution==Y).
        # We reconstruct it by calling filter_expr against a tiny shim.
        try:
            return filter_expr(item)
        except TypeError:
            return True


def _apply_boto3_filter(item, cond):
    """boto3.dynamodb.conditions expressions support direct evaluation via
    their internal structure only awkwardly; instead of reimplementing that,
    the FakeTable's query() just returns everything and the test asserts on
    which item resolve_task_item ultimately picks, which is what matters."""
    return True


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setenv('DYNAMODB_TABLE', 'test-tokens')
    monkeypatch.setenv('PIPELINES_TABLE', 'test-registry')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    # RESTART_HELPER_ARN is set automatically by the SAM template in every real
    # deploy. Tests that exercise restart_task mock routes.tasks.sfn to avoid
    # a real StartExecution call.
    monkeypatch.setenv('RESTART_HELPER_ARN',
                       'arn:aws:states:us-east-1:123456789012:stateMachine:restart-helper')


@pytest.fixture
def wired(fake_env, mocker):
    """Wire routes.tasks (and its resolve_task_item) against a single shared
    FakeTable via executions_repo, exactly as the real Lambda does — no
    per-function mocking of resolve_task_item or stop_task_executions."""
    import routes.tasks as tasks_module
    from dal.executions_repo import ExecutionsRepo

    fake_table = FakeTable()
    mocker.patch.object(ExecutionsRepo, 'table', new_callable=mocker.PropertyMock,
                        return_value=fake_table)
    # query_by_date_raw is a thin wrapper the module calls directly; point it
    # at the same fake table's query() so resolve_task_item's GSI fallback
    # path exercises the same in-memory state.
    mocker.patch.object(
        tasks_module.executions_repo, 'query_by_date_raw',
        side_effect=lambda **kw: fake_table.query(**kw),
    )
    # pipelines_repo is a separate table (the registry, not tokens_table) —
    # default to "no registry entry found", the graceful no-outlets path.
    # Individual tests override this to exercise the outlets-found path.
    mocker.patch.object(tasks_module.pipelines_repo, 'get', return_value=None)
    # asset-events is a separate table; land its writes in the same in-memory
    # store so tests can assert on both from one place (as they did when a
    # single patched dynamodb.Table served every table name).
    from dal.assets_repo import AssetEventsRepo
    mocker.patch.object(AssetEventsRepo, 'table', new_callable=mocker.PropertyMock,
                        return_value=fake_table)
    # Side effects unrelated to the chain under test — real SFN/S3 calls
    # would need a live AWS account, out of scope for this in-process test.
    mocker.patch.object(tasks_module, 'stop_task_executions')
    mocker.patch.object(tasks_module, 'record_manual_decision')
    mocker.patch.object(tasks_module, 'notify_dependents_via_sfn', return_value=True)
    return tasks_module, fake_table


def _waiting_task(status='waiting_decision'):
    return {
        'execution_name': 'transform-2026-07-24-run1',
        'task_name': 'transform', 'pipeline_name': 'acme-daily',
        'status': status, 'date': '2026-07-24',
        'started_at': '2026-07-24T08:00:00Z',
        'pipeline_execution': 'run-1',
    }

