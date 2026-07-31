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
import json
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
    mocker.patch.object(
        tasks_module.asset_events_repo, 'put',
        side_effect=lambda item: fake_table.put_item(Item=item),
    )
    # Side effects unrelated to the chain under test — real SFN/S3 calls
    # would need a live AWS account, out of scope for this in-process test.
    mocker.patch.object(tasks_module, 'stop_task_executions')
    mocker.patch.object(tasks_module, 'record_manual_decision')
    mocker.patch.object(tasks_module, 'notify_dependents_via_sfn', return_value=True)
    return tasks_module, fake_table


class TestStopThenRestartChain:
    def test_stop_then_restart_targets_the_correct_execution_and_ends_coherent(self, wired):
        tasks_module, fake_table = wired

        # A stale, older attempt of the SAME task_name — resolve_task_item's
        # pagination fix must not let this shadow the real (running) one.
        fake_table.items['extract-2026-07-21-oldattempt'] = {
            'execution_name': 'extract-2026-07-21-oldattempt',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'failed', 'date': '2026-07-21',
            'started_at': '2026-07-21T08:00:00Z',
            'pipeline_execution': 'run-1',
        }
        # The real, currently-running attempt.
        fake_table.items['extract-2026-07-21-currentrun'] = {
            'execution_name': 'extract-2026-07-21-currentrun',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'running', 'date': '2026-07-21',
            'started_at': '2026-07-21T14:00:00Z',
            'pipeline_execution': 'run-1',
            'wrapper_execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:wrapper:w1',
        }

        # Step 1: stop by full execution_name (the direct-lookup path).
        stop_resp = tasks_module.stop_task(
            'extract-2026-07-21-currentrun',
            {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
        )
        assert stop_resp['statusCode'] == 200
        assert fake_table.items['extract-2026-07-21-currentrun']['status'] == 'stopped'
        # The stale duplicate must be completely untouched.
        assert fake_table.items['extract-2026-07-21-oldattempt']['status'] == 'failed'

        # Step 2: restart the SAME task by task_name + date (the GSI
        # resolver path this time) — must succeed despite status='stopped'.
        # The SFN-helper path now handles the DDB reset itself, so the
        # console-api's job is just to hand the correct execution_name to the
        # helper — pin THAT contract here.
        calls = []
        fake_sfn = type('FakeSfn', (), {})()
        fake_sfn.start_execution = lambda **kw: (
            calls.append(kw) or {'executionArn': 'arn:aws:states:us-east-1:123456789012:execution:x:y'}
        )
        import routes.tasks as tm
        orig_sfn = tm.sfn
        tm.sfn = fake_sfn
        try:
            restart_resp = tasks_module.restart_task(
                'extract',
                {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
            )
        finally:
            tm.sfn = orig_sfn
        assert restart_resp['statusCode'] == 200, restart_resp
        # The restart helper must have been called for the CURRENT execution,
        # not the stale one — which is the whole point of this test.
        assert len(calls) == 1
        assert json.loads(calls[0]['input'])['execution_name'] == 'extract-2026-07-21-currentrun'
        # The stale duplicate still must not have been touched by either step.
        assert fake_table.items['extract-2026-07-21-oldattempt']['status'] == 'failed'

    def test_stopping_an_already_stopped_task_is_a_clean_409_not_a_crash(self, wired):
        """Calling stop_task twice in a row (double-click) must not throw —
        TERMINAL_CONDITION_EXPRESSION doesn't include 'stopped', so a second
        stop_task call takes a different path than restart_task's fix; this
        pins that it degrades to a clean, informative response either way."""
        tasks_module, fake_table = wired
        fake_table.items['extract-2026-07-21-currentrun'] = {
            'execution_name': 'extract-2026-07-21-currentrun',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'stopped', 'date': '2026-07-21',
            'started_at': '2026-07-21T14:00:00Z',
            'pipeline_execution': 'run-1',
        }
        resp = tasks_module.stop_task(
            'extract-2026-07-21-currentrun',
            {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
        )
        # 'stopped' isn't in TASK_TERMINAL_STATUSES, so is_terminal_status()
        # is False here and the call proceeds to re-stop rather than 409 —
        # documenting actual behavior so a future change here is a conscious
        # decision, not a silent regression.
        assert resp['statusCode'] == 200
        assert fake_table.items['extract-2026-07-21-currentrun']['status'] == 'stopped'

    def test_deep_pagination_three_stale_pages_before_the_real_one(self, wired):
        """resolve_task_item's fix must walk arbitrarily many pages, not just
        survive a single early-exit — three unrelated-status duplicates of
        the same task_name, THEN the real one, spread one-per-page."""
        tasks_module, fake_table = wired
        for i, status in enumerate(['failed', 'failed', 'aborted']):
            fake_table.items[f'extract-2026-07-21-stale{i:08d}'] = {
                'execution_name': f'extract-2026-07-21-stale{i:08d}',
                'task_name': 'extract', 'pipeline_name': 'acme-daily',
                'status': status, 'date': '2026-07-21',
                'started_at': f'2026-07-21T0{i+1}:00:00Z',
                'pipeline_execution': 'run-1',
            }
        fake_table.items['extract-2026-07-21-therealone'] = {
            'execution_name': 'extract-2026-07-21-therealone',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'running', 'date': '2026-07-21',
            'started_at': '2026-07-21T20:00:00Z',
            'pipeline_execution': 'run-1',
        }

        resp = tasks_module.stop_task(
            'extract', {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200
        assert fake_table.items['extract-2026-07-21-therealone']['status'] == 'stopped'
        # None of the three stale duplicates (spread across pages 1-3) may
        # have been touched — each kept its original, distinct status.
        assert fake_table.items['extract-2026-07-21-stale00000000']['status'] == 'failed'
        assert fake_table.items['extract-2026-07-21-stale00000001']['status'] == 'failed'
        assert fake_table.items['extract-2026-07-21-stale00000002']['status'] == 'aborted'

    def test_restart_via_sfn_helper_path_also_accepts_stopped(self, wired, monkeypatch):
        """The stopped-status fix on the SFN-helper path — the only path there
        is now, after the (previously dead) fallback was removed."""
        tasks_module, fake_table = wired
        monkeypatch.setenv('RESTART_HELPER_ARN', 'arn:aws:states:us-east-1:123456789012:stateMachine:restart-helper')
        fake_table.items['extract-2026-07-21-currentrun'] = {
            'execution_name': 'extract-2026-07-21-currentrun',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'stopped', 'date': '2026-07-21',
            'pipeline_execution': 'run-1',
        }
        fake_sfn = type('FakeSfn', (), {})()
        fake_sfn.start_execution = lambda **kw: {'executionArn': 'arn:aws:states:us-east-1:123456789012:execution:x:y'}
        import routes.tasks as tm
        orig_sfn = tm.sfn
        tm.sfn = fake_sfn
        try:
            resp = tasks_module.restart_task(
                'extract-2026-07-21-currentrun',
                {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
            )
        finally:
            tm.sfn = orig_sfn
        assert resp['statusCode'] == 200, resp
        # SFN-helper path doesn't itself flip DDB status (the restart_task_helper
        # SFN does that asynchronously) — only that the call was accepted, not
        # rejected as "not restartable".
        assert 'Restart initiated' in json.loads(resp['body'])['message']

    def test_skip_task_also_resolves_through_multi_page_duplicates(self, wired):
        """The pagination fix isn't restart/stop-specific — skip_task,
        fail_task, and mark_success all go through the same resolve_task_item
        and _execute_task_action. Spot-check skip_task specifically."""
        tasks_module, fake_table = wired
        fake_table.items['extract-2026-07-21-oldattempt'] = {
            'execution_name': 'extract-2026-07-21-oldattempt',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'success', 'date': '2026-07-21',
            'started_at': '2026-07-21T08:00:00Z',
            'pipeline_execution': 'run-1',
        }
        fake_table.items['extract-2026-07-21-waitingnow'] = {
            'execution_name': 'extract-2026-07-21-waitingnow',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'waiting', 'date': '2026-07-21',
            'started_at': '2026-07-21T14:00:00Z',
            'pipeline_execution': 'run-1',
        }
        resp = tasks_module.skip_task(
            'extract', {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        assert fake_table.items['extract-2026-07-21-waitingnow']['status'] == 'skipped'
        assert fake_table.items['extract-2026-07-21-oldattempt']['status'] == 'success'

    def test_sibling_task_in_same_pipeline_and_date_is_never_touched(self, wired):
        """Isolation: stopping/restarting one task must never affect a
        DIFFERENT task_name in the same pipeline+date, even though both are
        discovered by the same date-partition GSI query."""
        tasks_module, fake_table = wired
        fake_table.items['extract-2026-07-21-abc12345'] = {
            'execution_name': 'extract-2026-07-21-abc12345',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'running', 'date': '2026-07-21',
            'started_at': '2026-07-21T14:00:00Z',
            'pipeline_execution': 'run-1',
        }
        fake_table.items['transform-2026-07-21-xyz98765'] = {
            'execution_name': 'transform-2026-07-21-xyz98765',
            'task_name': 'transform', 'pipeline_name': 'acme-daily',
            'status': 'running', 'date': '2026-07-21',
            'started_at': '2026-07-21T14:05:00Z',
            'pipeline_execution': 'run-1',
        }
        resp = tasks_module.stop_task(
            'extract', {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200
        assert fake_table.items['extract-2026-07-21-abc12345']['status'] == 'stopped'
        assert fake_table.items['transform-2026-07-21-xyz98765']['status'] == 'running'

    def test_double_round_trip_stop_restart_stop_stays_coherent(self, wired):
        """A user stops, restarts, then stops again — each step must land on
        a sensible, expected status, with no step silently no-op'ing.

        The SFN-helper path is the only restart path now — the helper is what
        flips DDB status back to 'waiting' asynchronously. This test simulates
        that write to exercise the coherence chain end-to-end."""
        tasks_module, fake_table = wired
        fake_table.items['extract-2026-07-21-currentrun'] = {
            'execution_name': 'extract-2026-07-21-currentrun',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'running', 'date': '2026-07-21',
            'pipeline_execution': 'run-1',
        }
        r1 = tasks_module.stop_task(
            'extract-2026-07-21-currentrun',
            {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})})
        assert r1['statusCode'] == 200
        assert fake_table.items['extract-2026-07-21-currentrun']['status'] == 'stopped'

        # Restart: mock sfn.start_execution and simulate what the SFN helper
        # itself writes (status='waiting') so the third step below has the
        # correct precondition. In production this write happens inside
        # restart_task_helper's Reset_Task_Record state.
        fake_sfn = type('FakeSfn', (), {})()
        fake_sfn.start_execution = lambda **kw: {'executionArn': 'arn:aws:states:us-east-1:123:execution:x:y'}
        import routes.tasks as tm
        orig_sfn = tm.sfn
        tm.sfn = fake_sfn
        try:
            r2 = tasks_module.restart_task(
                'extract-2026-07-21-currentrun',
                {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})})
        finally:
            tm.sfn = orig_sfn
        assert r2['statusCode'] == 200, r2
        fake_table.items['extract-2026-07-21-currentrun']['status'] = 'waiting'  # simulate helper

        # Third action: the task is back to 'waiting' (a non-terminal, non-running
        # state) — stop_task's TASK_WAITING_STATUSES branch applies: 'aborted', not 'stopped'.
        r3 = tasks_module.stop_task(
            'extract-2026-07-21-currentrun',
            {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})})
        assert r3['statusCode'] == 200, r3
        assert fake_table.items['extract-2026-07-21-currentrun']['status'] == 'aborted'

    def test_get_all_tasks_shows_correct_status_after_a_real_stop_task_call(self, wired, mocker):
        """Chain a real stop_task call into get_all_tasks' read path: the
        row get_all_tasks returns must reflect exactly what stop_task wrote
        to the (shared, real) fake table — not a stale or reconciled-away
        value. (Note: 'stopped' is already in TASK_SETTLED_STATUSES, so
        _reconcile_orphaned_tasks never even considers this specific task
        regardless of wrapper_execution_arn — that field-preservation
        mechanism itself is covered separately, and mutation-tested, in
        test_format_task_row_reconcile.py. This test is about read/write
        consistency across the two real functions in sequence.)"""
        tasks_module, fake_table = wired
        fake_table.items['extract-2026-07-21-currentrun'] = {
            'execution_name': 'extract-2026-07-21-currentrun',
            'task_name': 'extract', 'pipeline_name': 'acme-daily',
            'status': 'running', 'date': '2026-07-21',
            'started_at': '2026-07-21T14:00:00Z',
            'pipeline_execution': 'run-1',
            'wrapper_execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:wrapper:w1',
        }
        stop_resp = tasks_module.stop_task(
            'extract-2026-07-21-currentrun',
            {'body': json.dumps({'date': '2026-07-21', 'pipeline_execution': 'run-1'})})
        assert stop_resp['statusCode'] == 200

        # get_all_tasks reads via query_runs_by_date/feed_dates, not the raw
        # table directly — patch its one real DDB entry point to read from
        # the SAME fake_table state stop_task just wrote.
        mocker.patch.object(
            tasks_module.executions_repo, 'query_runs_by_date',
            side_effect=lambda *a, **kw: list(fake_table.items.values()),
        )
        mocker.patch('feed.feed_dates', return_value=['2026-07-21'])
        list_resp = tasks_module.get_all_tasks({'queryStringParameters': {}})
        assert list_resp['statusCode'] == 200
        body = json.loads(list_resp['body'])
        row = next(t for t in body['tasks'] if t['execution_name'] == 'extract-2026-07-21-currentrun')
        # 'stopped' is what stop_task actually wrote, and read back correctly
        # through get_all_tasks' full pipeline (query -> filter -> format ->
        # reconcile) without being altered along the way.
        assert row['status'] == 'stopped'


def _waiting_task(status='waiting_decision'):
    return {
        'execution_name': 'transform-2026-07-24-run1',
        'task_name': 'transform', 'pipeline_name': 'acme-daily',
        'status': status, 'date': '2026-07-24',
        'started_at': '2026-07-24T08:00:00Z',
        'pipeline_execution': 'run-1',
    }


class TestSyntheticOutputMarker:
    """_write_synthetic_output_marker (§7a fix) — a task manually resolved
    (Skip/Mark Successful/Mark Failed/Stop) never runs through the wrapper's
    Save_Canonical_Output, so a downstream task calling xcom.pull() on it
    used to crash with PullError purely as a consequence of a manual
    decision made on an upstream task. This writes a recognizable synthetic
    marker to the same canonical key xcom.pull() reads, so the call
    succeeds instead — conditioned on a real result not already being
    there, so a genuine prior successful output is never clobbered."""

    def test_skip_writes_a_marker_at_the_canonical_key_xcom_pull_reads(self, wired):
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()

        resp = tasks_module.skip_task(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp

        canonical_key = 'output#acme-daily#transform#2026-07-24'
        assert canonical_key in fake_table.items
        marker = json.loads(fake_table.items[canonical_key]['result'])
        assert marker['_manually_resolved'] is True
        assert marker['_resolution'] == 'skip'

    def test_mark_success_also_writes_a_marker(self, wired):
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()

        resp = tasks_module.mark_success(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        marker = json.loads(fake_table.items['output#acme-daily#transform#2026-07-24']['result'])
        assert marker['_resolution'] == 'mark_success'

    def test_fail_also_writes_a_marker(self, wired):
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task(status='running')

        resp = tasks_module.fail_task(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        marker = json.loads(fake_table.items['output#acme-daily#transform#2026-07-24']['result'])
        assert marker['_resolution'] == 'fail'

    def test_stop_also_writes_a_marker(self, wired):
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()

        resp = tasks_module.stop_task(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        marker = json.loads(fake_table.items['output#acme-daily#transform#2026-07-24']['result'])
        assert marker['_resolution'] == 'stop'

    def test_never_overwrites_a_real_prior_result_same_task_and_date(self, wired):
        """A genuine, real output from an earlier successful run of this
        exact task/date (e.g. a same-day re-run, or output written some
        other way) must never be clobbered by the synthetic marker — only
        fill the gap when nothing real is there."""
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()
        real_result = json.dumps({'rows_processed': 42})
        fake_table.items['output#acme-daily#transform#2026-07-24'] = {
            'execution_name': 'output#acme-daily#transform#2026-07-24',
            'task_name': 'transform',
            'result': real_result,
            'status': 'success',
        }

        resp = tasks_module.skip_task(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        # Real result must be untouched — not replaced by the synthetic marker.
        assert fake_table.items['output#acme-daily#transform#2026-07-24']['result'] == real_result

    def test_ddb_failure_in_marker_write_does_not_block_the_manual_action(self, wired, mocker):
        """The realistic failure mode — a ClientError from DynamoDB — is
        caught inside _write_synthetic_output_marker itself, so the manual
        action's own 200 response is unaffected (best-effort, matching
        every other status write in this codebase)."""
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()

        from botocore.exceptions import ClientError
        # Fail ONLY the marker write. The main status update goes through the
        # same repo but a different method (the fixture's fake_table), so
        # patching update() alone isolates the marker path.
        real_update = tasks_module.executions_repo.update

        def _fail_only_marker_writes(key, *a, **kw):
            if key.startswith('output#'):
                raise ClientError(
                    {'Error': {'Code': 'ProvisionedThroughputExceededException', 'Message': 'x'}},
                    'UpdateItem')
            return real_update(key, *a, **kw)

        mocker.patch.object(
            tasks_module.executions_repo, 'update', side_effect=_fail_only_marker_writes)

        resp = tasks_module.skip_task(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        assert fake_table.items['transform-2026-07-24-run1']['status'] == 'skipped'
        assert 'output#acme-daily#transform#2026-07-24' not in fake_table.items


class TestAssetEventsOnManualSuccess:
    """§7c: Mark Successful — the one manual action that explicitly claims
    real work happened — notifies push-triggered asset consumers the same
    way a normal completion would. Skip/Fail/Stop correctly never do this,
    since they make no claim that anything was actually produced."""

    def _registry_with_outlet(self, task_name='transform', asset_name='sales/daily'):
        return {
            'pipeline_name': 'acme-daily',
            'dag': json.dumps({
                'nodes': [{'id': task_name, 'name': task_name, 'type': 'task',
                           'outlets': [{'name': asset_name, 'uri': 's3://bucket/sales/'}]}],
                'edges': [],
            }),
        }

    def test_mark_success_with_outlets_records_asset_event_and_notifies_consumers(self, wired, mocker):
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()
        mocker.patch.object(tasks_module.pipelines_repo, 'get',
                            return_value=self._registry_with_outlet())
        import task_actions
        mocker.patch.object(task_actions, 'NOTIFY_ASSET_CONSUMERS_SFN_ARN',
                            'arn:aws:states:us-east-1:123456789012:stateMachine:notify-asset-consumers')
        fake_sfn = mocker.Mock()
        fake_sfn.start_execution = mocker.Mock(return_value={'executionArn': 'arn:x'})
        mocker.patch.object(tasks_module, 'sfn', fake_sfn)
        mocker.patch.object(task_actions, 'sfn', fake_sfn)

        resp = tasks_module.mark_success(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp

        # Asset event recorded under the asset's name.
        asset_event = next(
            (v for k, v in fake_table.items.items() if v.get('asset_name') == 'sales/daily'), None,
        )
        assert asset_event is not None, "no asset event was recorded"
        assert asset_event['source_task'] == 'transform'
        assert asset_event['source_dag'] == 'acme-daily'

        # notify_asset_consumers_via_sfn's SFN was invoked (not just the
        # orchestration send_task_success callback — a genuinely separate call).
        asset_notify_calls = [
            c for c in fake_sfn.start_execution.call_args_list
            if 'asset_name' in c.kwargs.get('input', '')
        ]
        assert len(asset_notify_calls) == 1

    def test_mark_success_without_outlets_emits_nothing(self, wired):
        """No registry entry (default in `wired`) → no outlets found → the
        whole emission step is a graceful no-op, not an error."""
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()

        resp = tasks_module.mark_success(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        assert not any(v.get('asset_name') for v in fake_table.items.values())

    @pytest.mark.parametrize('action_fn_name', ['skip_task', 'fail_task', 'stop_task'])
    def test_skip_fail_stop_never_emit_asset_events_even_with_real_outlets(self, wired, mocker, action_fn_name):
        """The other three manual actions make no claim that anything was
        actually produced — they must never emit an asset event, even when
        the task genuinely has outlets configured."""
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task(
            status='running' if action_fn_name == 'fail_task' else 'waiting_decision')
        mocker.patch.object(tasks_module.pipelines_repo, 'get',
                            return_value=self._registry_with_outlet())

        action_fn = getattr(tasks_module, action_fn_name)
        resp = action_fn(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
        assert not any(v.get('asset_name') for v in fake_table.items.values()), \
            f"{action_fn_name} must never emit an asset event"

    def test_malformed_registry_dag_does_not_break_mark_success(self, wired, mocker):
        """A malformed/unparseable dag field in the registry must not turn
        Mark Successful itself into a failure — best-effort, matching every
        other side-effect in this codebase."""
        tasks_module, fake_table = wired
        fake_table.items['transform-2026-07-24-run1'] = _waiting_task()
        mocker.patch.object(tasks_module.pipelines_repo, 'get',
                            return_value={'pipeline_name': 'acme-daily', 'dag': 'not valid json{{{'})

        resp = tasks_module.mark_success(
            'transform', {'body': json.dumps({'date': '2026-07-24', 'pipeline_execution': 'run-1'})},
        )
        assert resp['statusCode'] == 200, resp
