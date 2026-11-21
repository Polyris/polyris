"""
Tests for alerting architecture redesign (v64).

Validates:
- run_task: backfill suppression, conditional Slack/PagerDuty, alerts_json in DDB
- failure_handler: upstream_failed suppression, PagerDuty removed, empty token Slack
- restart_task: alerts field passed from DDB
- main.tf: pagerduty_alerter_arn wiring
"""

import json
import os
import re
import sys

import pytest

# sys.path setup moved to conftest.py

TEMPLATES = os.path.join(
    os.path.dirname(__file__), '..', '..', 'sam', 'sfn_templates'
)

def load(helper_name):
    path = os.path.join(TEMPLATES, 'helpers', helper_name, 'sfn.tpl.json')
    with open(path) as f:
        content = f.read()
    # Template variables that appear as bare (unquoted) values break JSON
    # parsing. E.g.: "HeartbeatSeconds": ${pause_heartbeat_seconds},
    # Quoted vars like "${tokens_table}" are valid JSON strings — no fix needed.
    content = re.sub(r':\s*\$\{(\w+)\}(\s*[,}\]])', r': 99999\2', content)
    return json.loads(content)


# ============================================================
# run_task — failure flow structure
# ============================================================

class TestRunTaskAlertingFlow:
    """The failure flow of 1.3 (ADR #137 §1): retries, then Get_Decision_Timeout → Ask (waiting_decision) →
    Check_Is_Backfill → Interactive_Slack → Send_PagerDuty_Alert → the end. No process waits for the
    decision: the dispatcher fails the row at its deadline."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_exhausted_retries_read_the_decision_timeout_then_ask(self):
        assert self.rt['States']['Check_Should_Retry']['Default'] == 'Get_Decision_Timeout'
        assert self.rt['States']['Get_Decision_Timeout']['Next'] == 'Ask'
        assert self.rt['States']['Ask']['Next'] == 'Check_Is_Backfill'

    def test_get_decision_timeout_reads_global_settings(self):
        """Get_Decision_Timeout reads the reserved __global_settings__ registry
        record and assigns decision_timeout_seconds (default 18000 on miss)."""
        state = self.rt['States']['Get_Decision_Timeout']
        assert state['Resource'] == 'arn:aws:states:::dynamodb:getItem'
        assert state['Arguments']['Key']['pipeline_name']['S'] == '__global_settings__'
        assert 'decision_timeout_seconds' in state['Assign']
        # graceful: the default timeout, and on to the decision
        assert state['Catch'][0]['Next'] == 'Ask'
        assert state['Catch'][0]['Assign']['decision_timeout_seconds'] == 18000

    def test_the_decision_records_its_timeout_and_nothing_waits_for_it(self):
        """The row carries the timeout in force; no Wait state holds the execution for the decision."""
        values = self.rt['States']['Ask']['Arguments']['ExpressionAttributeValues']
        assert 'decision_timeout_seconds' in values[':timeout']['N']
        assert 'decision_timeout_seconds' in values[':deadline']['S']
        assert 'Wait_For_Decision' not in self.rt['States']
        assert self.rt['States']['Waiting_For_Decision']['Type'] == 'Succeed'

    def test_backfill_skips_the_alerts(self):
        """Backfill runs skip alerting; the decision still waits in the console."""
        state = self.rt['States']['Check_Is_Backfill']
        assert state['Type'] == 'Choice'
        assert state['Choices'][0]['Next'] == 'Waiting_For_Decision'
        assert 'is_backfill' in state['Choices'][0]['Condition']

    def test_backfill_default_goes_to_interactive_slack(self):
        """Non-backfill runs proceed directly to Interactive_Slack. No per-channel gate:
        the notify Lambda self-reads alert_config from DDB and no-ops when unconfigured."""
        state = self.rt['States']['Check_Is_Backfill']
        assert state['Default'] == 'Interactive_Slack'

    def test_wrapper_alert_gates_removed(self):
        """ADR #103 Stage 2: the deprecated wrapper-field Choice gates
        (Check_Has_Slack / Check_Has_PagerDuty) are gone — config lives in DDB alert_config."""
        assert 'Check_Has_Slack' not in self.rt['States']
        assert 'Check_Has_PagerDuty' not in self.rt['States']

    def test_interactive_slack_goes_to_pagerduty(self):
        """Interactive_Slack → Send_PagerDuty_Alert. Both channels attempted unconditionally;
        each notifier no-ops if its channel is unconfigured (or absent in the OSS build)."""
        state = self.rt['States']['Interactive_Slack']
        assert state['Next'] == 'Send_PagerDuty_Alert'

    def test_slack_failure_goes_to_pagerduty(self):
        """Save_Slack_Failed → Send_PagerDuty_Alert (Next, and Catch after the generation fence)."""
        state = self.rt['States']['Save_Slack_Failed']
        assert state['Next'] == 'Send_PagerDuty_Alert'
        assert state['Catch'][0]['Next'] == 'Superseded'                # Rule 47
        assert state['Catch'][-1]['Next'] == 'Send_PagerDuty_Alert'

    def test_run_task_reads_no_wrapper_alerts_field(self):
        """ADR #103 Stage 2: no state in run_task reads the deprecated wrapper `alerts` field."""
        assert 'states.input.alerts' not in json.dumps(self.rt)

    def test_pagerduty_alert_ends_the_execution(self):
        """Send_PagerDuty_Alert, and its failure, end at Waiting_For_Decision (graceful degradation)."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert state['Next'] == 'Waiting_For_Decision'
        assert state['Catch'][0]['Next'] == 'Waiting_For_Decision'

    def test_pagerduty_alert_has_retry(self):
        """Send_PagerDuty_Alert has Retry for transient errors."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert 'Retry' in state

    def test_pagerduty_alert_invokes_notify_lambda(self):
        """Send_PagerDuty_Alert invokes the notify Lambda with the live action
        (Stage 2: the posting moved out of the pagerduty_alerter helper SFN)."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert state['Resource'] == 'arn:aws:states:::lambda:invoke'
        assert '${notify_function_arn}' in state['Arguments']['FunctionName']
        assert state['Arguments']['Payload']['action'] == 'live_pagerduty'

    def test_pagerduty_alert_passes_pipeline_name(self):
        """The notify Lambda reads severity/routing_key from alert_config by
        pipeline_name (Stage 2), so the payload carries pipeline_name + failure,
        not the severity inline."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        payload = state['Arguments']['Payload']
        assert 'pipeline_name' in payload
        assert 'pipeline_name' in payload['failure']
        assert 'task_name' in payload['failure']

    def test_pagerduty_alert_preserves_input(self):
        """Send_PagerDuty_Alert preserves input via Output."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert '$states.input' in state.get('Output', '')

    @pytest.mark.parametrize("task_state", [
        'Run_Task_SFN', 'Run_Task_Lambda', 'Run_Task_Glue',
        'Run_Task_ECS', 'Run_Task_Athena', 'Run_Task_EMR', 'Run_Task_Batch'
    ])
    def test_every_task_type_fails_into_the_retry_decision(self, task_state):
        """All Run_Task_* states catch to the retry decision (ADR #106), which on exhausted retries
        converges on the decision of ADR #114."""
        state = self.rt['States'][task_state]
        assert {c['Next'] for c in state['Catch']} == {'Check_Should_Retry'}


class TestRetryLoopComposesWithHumanDecision:
    """ADR #106/#107: the auto-retry loop and the human-in-the-loop decision compose SEQUENTIALLY and in
    ISOLATION. Retries run and exhaust inside one run_task execution; only then does the row wait for a
    decision. An operator's Restart raises the generation and starts a new run_task, whose claim resets
    retry_attempt to 0; the decision flow never touches the counter."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    # the states allowed to read/write the retry counter: the claims that reset it, the loop, and the
    # timeline events that carry it (ADR #125).
    RETRY_LOOP_STATES = {
        'Claim', 'Read_Claim', 'Check_Should_Retry', 'Prepare_Retry_Wait', 'Save_Retry_Wait_Delay',
        'Wait_Before_Retry', 'Increment_Retry', 'Save_Retry_Attempt', 'Emit_Task_Retry_Started',
        'Emit_Task_Started', 'Emit_Task_Finished_Success',
    }

    @classmethod
    def _targets(cls, body):
        """Every Next/Default transition target anywhere inside a state body
        (covers Choices and Catch, not just the top-level Next)."""
        out = set()
        if isinstance(body, dict):
            for k, v in body.items():
                if k in ('Next', 'Default') and isinstance(v, str):
                    out.add(v)
                else:
                    out |= cls._targets(v)
        elif isinstance(body, list):
            for it in body:
                out |= cls._targets(it)
        return out

    def test_retry_exhaustion_hands_off_to_decision_flow(self):
        """Exhausted retries fall through Check_Should_Retry's Default into the
        human-decision entry — retries happen strictly BEFORE any human decision."""
        assert self.rt['States']['Check_Should_Retry']['Default'] == 'Get_Decision_Timeout'

    def test_decision_flow_never_touches_retry_counter(self):
        """No state outside the retry loop references retry_attempt, so the decision
        flow (and a human Restart) can neither read nor reset the auto-retry counter."""
        for name, body in self.rt['States'].items():
            if name in self.RETRY_LOOP_STATES:
                continue
            assert 'retry_attempt' not in json.dumps(body), \
                f"{name} references retry_attempt — breaks retry/HITL isolation"

    def test_only_the_start_and_the_retry_loop_enter_the_attempt(self):
        """An attempt begins at Read_Upstream_Outputs: from the start (Emit_Task_Started) or from the retry
        loop's closure (Emit_Task_Retry_Started). No decision-flow state loops back into the work; a human
        Restart arrives as a new execution. The work itself (Check_Task_Type) is entered only from the
        input record of its attempt."""
        feeders = {n for n, b in self.rt['States'].items() if 'Read_Upstream_Outputs' in self._targets(b)}
        assert feeders == {'Emit_Task_Started', 'Emit_Task_Retry_Started'}
        dispatchers = {n for n, b in self.rt['States'].items() if 'Check_Task_Type' in self._targets(b)}
        assert dispatchers == {'Save_Input_Record'}

    def test_the_counter_is_reset_by_the_claim_before_the_first_attempt(self):
        """Claim and Read_Claim assign retry_attempt 0 before Emit_Task_Started, so every start, a human
        Restart's included, begins the loop clean."""
        for name in ('Claim', 'Read_Claim'):
            assert self.rt['States'][name]['Assign']['retry_attempt'] == 0


# ============================================================
# failure_handler — upstream_failed suppression
# ============================================================

class TestFailureHandlerUpstreamFailed:
    """Verify upstream_failed tasks skip alerting."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fh = load('failure_handler')

    def test_check_is_upstream_failed_exists(self):
        """Check_Is_Upstream_Failed state exists."""
        assert 'Check_Is_Upstream_Failed' in self.fh['States']

    def test_upstream_failed_skips_slack(self):
        """upstream_failed → Check_Orchestration_Token (skips Slack)."""
        state = self.fh['States']['Check_Is_Upstream_Failed']
        assert state['Choices'][0]['Next'] == 'Check_Orchestration_Token'
        assert 'UpstreamFailed' in state['Choices'][0]['Condition']

    def test_non_upstream_goes_to_send_alerts(self):
        """Normal failure → Send_Alerts (ADR #103 Stage-1: one Lambda call)."""
        state = self.fh['States']['Check_Is_Upstream_Failed']
        assert state['Default'] == 'Send_Alerts'

    def test_notify_dependents_goes_to_upstream_check(self):
        """Notify_Dependents → Check_Is_Upstream_Failed."""
        state = self.fh['States']['Notify_Dependents']
        assert state['Next'] == 'Check_Is_Upstream_Failed'

    def test_notify_dependents_catch_goes_to_upstream_check(self):
        """Notify_Dependents catch → Check_Is_Upstream_Failed (graceful)."""
        state = self.fh['States']['Notify_Dependents']
        assert state['Catch'][0]['Next'] == 'Check_Is_Upstream_Failed'


# ============================================================
# failure_handler — PagerDuty removed
# ============================================================

class TestFailureHandlerNoPagerDuty:
    """Verify PagerDuty ALERT states are removed from failure_handler.
    
    PD alerting is in run_task (line 1). failure_handler only does Slack + orchestration.
    PD resolve on human decisions is handled by Lambda API (slack.py, tasks.py).
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fh = load('failure_handler')

    def test_no_pagerduty_alert_states(self):
        """PagerDuty ALERT states removed from failure_handler."""
        for name in ['Check_PagerDuty_Alert', 'Send_PagerDuty_Alert', 'Save_PagerDuty_Failed']:
            assert name not in self.fh['States'], f"{name} should be removed"

    def test_no_pagerduty_alerter_arn(self):
        """pagerduty_alerter_arn not referenced in failure_handler."""
        content = json.dumps(self.fh)
        assert 'pagerduty_alerter_arn' not in content

    def test_no_pagerduty_resolver(self):
        """No PD resolve in failure_handler — resolve is in Lambda API handlers."""
        content = json.dumps(self.fh)
        assert 'pagerduty_resolver_arn' not in content
        assert 'Resolve_PagerDuty' not in content

    def test_send_alerts_state_present(self):
        """ADR #103 Stage-1: one Send_Alerts state replaces the 3-state fan-out."""
        assert 'Send_Alerts' in self.fh['States']

    def test_old_slack_chain_removed(self):
        """The hard-wired Slack chain is gone (channels are data now)."""
        for name in ['Check_Slack_Alert', 'Send_Slack_Alert', 'Record_Slack_Failure']:
            assert name not in self.fh['States'], f"{name} should be removed"

    def test_old_fanout_states_removed(self):
        """The 3 SFN alert states collapsed into the Lambda (Stage-1)."""
        for name in ['Get_Alert_Config', 'Has_Channels', 'Fan_Out_Alerts']:
            assert name not in self.fh['States'], f"{name} should be gone"

    def test_send_alerts_invokes_notify_lambda(self):
        """Send_Alerts is a single Lambda invoke (the Lambda fans out itself)."""
        state = self.fh['States']['Send_Alerts']
        assert 'lambda:invoke' in state['Resource']
        assert '${notify_function_arn}' in json.dumps(state)

    def test_send_alerts_passes_pipeline_and_failure(self):
        """The Lambda gets pipeline_name + the failure; it reads config itself."""
        payload = self.fh['States']['Send_Alerts']['Arguments']['Payload']
        assert 'pipeline_name' in payload
        assert 'failure' in payload

    def test_send_alerts_never_blocks_callback(self):
        """Delivery problems must not block the orchestration callback."""
        state = self.fh['States']['Send_Alerts']
        assert state['Next'] == 'Check_Orchestration_Token'
        assert state['Catch'][0]['Next'] == 'Check_Orchestration_Token'


# ============================================================
# failure_handler — no buttons on dead task
# ============================================================

class TestFailureHandlerSlackNoButtons:
    """ADR #103: failure_handler no longer sends Slack directly, so the old
    "empty token to suppress buttons on a dead task" concern moves to the notify
    Lambda. Interactive buttons remain only in run_task's Interactive_Slack
    (live tasks), which is unchanged."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fh = load('failure_handler')

    def test_failure_handler_has_no_interactive_slack(self):
        """No waitForTaskToken Slack in failure_handler — a failed task is dead,
        nothing to interact with. Fan-out delivers a plain notification."""
        content = json.dumps(self.fh)
        assert 'Send_Slack_Alert' not in content
        assert 'waitForTaskToken' not in content

    def test_send_alerts_failure_context_has_no_token(self):
        """The failure context passed to notify carries no orchestration token —
        notifiers post a plain message, not interactive buttons."""
        payload = self.fh['States']['Send_Alerts']['Arguments']['Payload']
        assert 'token' not in payload['failure']


# ============================================================
# restart_task — alerts field
# ============================================================

class TestMainTFWiring:
    """Verify SAM template variable wiring."""

    @pytest.fixture(autouse=True)
    def setup(self):
        main_tf = os.path.join(
            os.path.dirname(__file__), '..', '..', 'sam', 'template.yaml'
        )
        with open(main_tf) as f:
            self.content = f.read()

    def test_run_task_invokes_notify_not_alerter(self):
        """Stage 3: RunTaskHelperSfn no longer references the deleted
        pagerduty_alerter; it passes notify_function_arn instead (Stage 2 wiring)."""
        idx = self.content.find('RunTaskHelperSfn:')
        assert idx != -1, "RunTaskHelperSfn not found in SAM template"
        block = self.content[idx:idx+3000]
        assert 'pagerduty_alerter_arn' not in block, "alerter ref should be gone (Stage 3)"
        assert 'notify_function_arn' in block, "notify_function_arn must be present"

    def test_failure_handler_no_pagerduty_alerter(self):
        """FailureHandlerSfn does not reference the deleted pagerduty_alerter;
        it sends alerts via the notify Lambda (Send_Alerts, Stage 1a)."""
        idx = self.content.find('FailureHandlerSfn:')
        assert idx != -1, "FailureHandlerSfn not found in SAM template"
        block = self.content[idx:idx+3000]
        assert 'pagerduty_alerter_arn' not in block


class TestCrossTemplateConsistency:
    """Verify templates are consistent with each other."""

    def test_run_task_pagerduty_dedup_matches_wrapper_resolve(self):
        """Live alert (run_task) and resolve (wrapper) must produce the same PD
        dedup_key. Both now invoke the notify Lambda, which builds the key from
        failure.{pipeline_name,task_name,date}; so both payloads must carry those
        three fields in failure (the Lambda derives pipeline/task/date identically)."""
        import json as _json
        import re as _re
        rt = load('run_task')
        # dependency_wrapper lives one level up from helpers/, load it directly.
        _dw_path = os.path.join(TEMPLATES, 'dependency_wrapper', 'sfn.tpl.json')
        _dw_raw = open(_dw_path).read()
        _dw_clean = _re.sub(r'\{%[^%]*%\}', 'J', _re.sub(r'\$\{[a-z_]+\}', '0', _dw_raw))
        dw = _json.loads(_dw_clean)
        live = rt['States']['Send_PagerDuty_Alert']['Arguments']['Payload']['failure']
        resolve = dw['States']['Resolve_PagerDuty']['Arguments']['Payload']['failure']
        for field in ('pipeline_name', 'task_name'):
            assert field in live, f'live missing {field}'
            assert field in resolve, f'resolve missing {field}'
        # date drives the dedup key — both must reference input.date
        assert 'date' in live and 'date' in resolve

    def test_failure_handler_all_states_reachable(self):
        """All failure_handler states reachable from StartAt."""
        fh = load('failure_handler')
        reachable = set()
        queue = [fh['StartAt']]
        while queue:
            name = queue.pop(0)
            if name in reachable or name not in fh['States']:
                continue
            reachable.add(name)
            state = fh['States'][name]
            for ref in [state.get('Next'), state.get('Default')]:
                if ref:
                    queue.append(ref)
            for c in state.get('Choices', []):
                if c.get('Next'):
                    queue.append(c['Next'])
            for c in state.get('Catch', []):
                if c.get('Next'):
                    queue.append(c['Next'])

        unreachable = set(fh['States'].keys()) - reachable
        assert not unreachable, f"Unreachable states: {unreachable}"

    def test_run_task_all_states_reachable(self):
        """All run_task states reachable from StartAt."""
        rt = load('run_task')
        reachable = set()
        queue = [rt['StartAt']]
        while queue:
            name = queue.pop(0)
            if name in reachable or name not in rt['States']:
                continue
            reachable.add(name)
            state = rt['States'][name]
            for ref in [state.get('Next'), state.get('Default')]:
                if ref:
                    queue.append(ref)
            for c in state.get('Choices', []):
                if c.get('Next'):
                    queue.append(c['Next'])
            for c in state.get('Catch', []):
                if c.get('Next'):
                    queue.append(c['Next'])
            # Map ItemProcessor inner states
            ip = state.get('ItemProcessor', {}).get('States', {})
            for inner_state in ip.values():
                for ref in [inner_state.get('Next')]:
                    if ref and ref in ip:
                        pass  # inner states only reference inner states

        unreachable = set(rt['States'].keys()) - reachable
        assert not unreachable, f"Unreachable states: {unreachable}"

    def test_no_dangling_references(self):
        """No state references point to non-existent states (both templates)."""
        for name in ['run_task', 'failure_handler', 'restart_task']:
            data = load(name)
            all_states = set(data['States'].keys())
            for sname, state in data['States'].items():
                for ref in [state.get('Next'), state.get('Default')]:
                    if ref:
                        assert ref in all_states, \
                            f"{name}: {sname} → {ref} does not exist"
                for c in state.get('Choices', []):
                    if c.get('Next'):
                        assert c['Next'] in all_states, \
                            f"{name}: {sname} choice → {c['Next']} does not exist"
                for c in state.get('Catch', []):
                    if c.get('Next'):
                        assert c['Next'] in all_states, \
                            f"{name}: {sname} catch → {c['Next']} does not exist"


# ============================================================
# Slack mentions — DSL validation
# ============================================================

# ============================================================
# Slack mentions — SFN template flow
# ============================================================

class TestSlackMentionsTemplateFlow:
    """Verify slack_mentions flows through SFN templates."""

    def test_run_task_interactive_slack_invokes_notify(self):
        """Interactive_Slack now invokes the notify Lambda (Stage 2). Mentions and
        channel come from alert_config read by the Lambda, so the payload carries
        pipeline_name + console_api_endpoint + failure, not mentions inline."""
        rt = load('run_task')
        state = rt['States']['Interactive_Slack']
        assert state['Resource'] == 'arn:aws:states:::lambda:invoke'
        payload = state['Arguments']['Payload']
        assert payload['action'] == 'interactive_slack'
        assert 'pipeline_name' in payload
        assert 'console_api_endpoint' in payload

    def test_failure_handler_send_alerts_passes_pipeline(self):
        """ADR #103 Stage-1: failure_handler no longer routes channel/config in the
        SFN — it passes pipeline_name + failure, and the notify Lambda reads the
        config (incl. mentions) from the registry itself."""
        fh = load('failure_handler')
        payload = fh['States']['Send_Alerts']['Arguments']['Payload']
        assert 'pipeline_name' in payload
        assert 'failure' in payload
        # the old per-channel routing + Slack chain are gone from the SFN
        assert 'Send_Slack_Alert' not in fh['States']
        assert 'Fan_Out_Alerts' not in fh['States']

    def test_no_hardcoded_responsible_for_anywhere(self):
        """No template references responsible_for_data_pipeline_ops."""
        for name in ['run_task', 'failure_handler']:
            data = load(name)
            content = json.dumps(data)
            assert 'responsible_for_data_pipeline_ops' not in content, \
                f"{name} still has hardcoded responsible_for"


# ============================================================
# PagerDuty alerter — links to AWS Console
# ============================================================

class TestRunTaskPagerDutyPayload:
    """Stage 2: run_task invokes the notify Lambda for the live PD alert. The
    Lambda reads alert_config from the registry and builds the PD payload itself,
    so run_task only passes pipeline_name + failure context (execution ARNs are no
    longer threaded through — the Lambda resolves links from the failure)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_invokes_notify_with_live_action(self):
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert state['Resource'] == 'arn:aws:states:::lambda:invoke'
        assert state['Arguments']['Payload']['action'] == 'live_pagerduty'

    def test_payload_carries_failure_context(self):
        payload = self.rt['States']['Send_PagerDuty_Alert']['Arguments']['Payload']
        failure = payload['failure']
        for field in ('pipeline_name', 'task_name', 'execution_name', 'error', 'date'):
            assert field in failure, f'failure missing {field}'

    def test_payload_passes_pipeline_name_for_config_read(self):
        # The Lambda reads alert_config by pipeline_name, so it must be present.
        payload = self.rt['States']['Send_PagerDuty_Alert']['Arguments']['Payload']
        assert 'pipeline_name' in payload


class TestCanonicalOutput:
    """The canonical output: a stable key for upstream reads, which survives incremental backfill. In 1.3
    the success writes it in its own transaction (ADR #137 §1, invariant 12), so it is not best effort."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_the_success_writes_the_output_under_the_stable_key_in_its_transaction(self):
        """output#pipeline#task#date — no run-specific parts; status, result, task_name and ttl."""
        record = self.rt['States']['Record_Success']['Arguments']
        assert "'output#' & $states.context.Execution.Input.pipeline_name & '#' & " \
               "$states.context.Execution.Input.task_name & '#' & $states.context.Execution.Input.date" in record
        assert 'pipeline_execution_short' not in record
        for clause in ('task_name = :tn', '#r = :r', '#s = :success', '#ttl = :ttl'):
            assert clause in record, clause
        assert 'task_input' not in record          # it lives in the input# record (Save_Input_Record)

    def test_read_upstream_uses_canonical_key(self):
        """Read_Upstream_Outputs reads by output#pipeline#dep#date."""
        get_dep = self.rt['States']['Read_Upstream_Outputs']['ItemProcessor']['States']['Get_Dep_Output']
        key = get_dep['Arguments']['Key']['execution_name']['S']
        assert 'output#' in key
        assert 'pipeline_name' in key


class TestSaveInputRecordSeparate:
    """Save_Input_Record writes task_input (upstream + variables) to a SEPARATE
    DDB record with key 'input#{pipeline}#{task}#{date}'. Split from the old
    Save_Task_Input state so the Console preview is bounded by DDB's 400KB item
    limit (not the arbitrary 25KB cap that used to sit on the shared output#
    row)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_uses_input_key_prefix_not_output(self):
        """The new prefix keeps task_input out of the output# row entirely,
        eliminating the 25KB shared-budget truncation that caused
        _upstream_omitted markers in the Console Input tab."""
        state = self.rt['States']['Save_Input_Record']
        assert state['Resource'] == 'arn:aws:states:::dynamodb:updateItem'
        key = state['Arguments']['Key']['execution_name']['S']
        assert "'input#'" in key or '"input#"' in key
        assert 'pipeline_name' in key
        assert 'task_name' in key
        assert 'date' in key

    def test_stores_task_input_field_without_25kb_truncation(self):
        """The old Save_Task_Input truncated at 25000 chars to fit alongside
        the 'result' field. Split records eliminate the shared budget so this
        state writes the full task_input blob (bounded only by DDB's 400KB item)."""
        state = self.rt['States']['Save_Input_Record']
        update_expr = state['Arguments']['UpdateExpression']
        assert 'task_input = :ti' in update_expr
        ti_expr = state['Arguments']['ExpressionAttributeValues'][':ti']['S']
        # No more $length($ti) > 25000 ? {_upstream_omitted: true, ...} : ...
        assert '25000' not in ti_expr
        assert '_upstream_omitted' not in ti_expr

    def test_uses_task_date_field_not_date_to_avoid_gsi_pollution(self):
        """Deliberately mismatched field name: date-pipeline-index GSI keys on
        'date' + 'pipeline_name'. Writing 'date' here would populate the GSI
        with internal input# rows. 'task_date' keeps the same semantic value
        (task run date) without polluting execution listings. Belt-and-suspenders
        with is_internal_record()'s input#* prefix filter (utils.py)."""
        state = self.rt['States']['Save_Input_Record']
        update_expr = state['Arguments']['UpdateExpression']
        assert 'task_date = :td' in update_expr
        # Must NOT write bare 'date =' — that would show up in the GSI
        # (allow 'task_date' via startswith exclusion)
        for part in update_expr.split(','):
            part = part.strip()
            assert not part.startswith('date '), \
                f"Save_Input_Record must not write bare 'date' field (GSI pollution): {part}"

    def test_never_writes_result_or_status(self):
        """A task-start write, not a completion write: result/status belong to the success transaction."""
        state = self.rt['States']['Save_Input_Record']
        update_expr = state['Arguments']['UpdateExpression']
        assert 'result' not in update_expr
        assert 'status' not in update_expr

    def test_ttl_counts_from_each_write(self):
        """D82/D108: input# keeps 90 days from its latest write, the console's window of runs."""
        update_expr = self.rt['States']['Save_Input_Record']['Arguments']['UpdateExpression']
        assert '#ttl = :ttl' in update_expr and 'if_not_exists' not in update_expr

    def test_is_best_effort_falls_through_to_check_task_type(self):
        """DDB failure here, or a record too large, must not block dispatch."""
        catch = self.rt['States']['Save_Input_Record']['Catch']
        assert [c['ErrorEquals'] for c in catch] == [['States.DataLimitExceeded'], ['States.ALL']]
        assert {c['Next'] for c in catch} == {'Check_Task_Type'}


class TestUpstreamDataFlow:
    """Verify upstream output reads, truncation, and how they're passed into
    each task type's own child invocation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_read_upstream_uses_canonical_key(self):
        """Get_Dep_Output reads by output#pipeline#dep#date — no run-specific parts."""
        get_dep = self.rt['States']['Read_Upstream_Outputs']['ItemProcessor']['States']['Get_Dep_Output']
        key = get_dep['Arguments']['Key']['execution_name']['S']
        assert 'output#' in key
        assert 'pipeline_name' in key
        assert '$states.input.name' in key
        assert 'date' in key
        assert 'pipeline_execution_short' not in key
        assert get_dep['Arguments']['ConsistentRead'] is True     # its success wrote it in one transaction

    def test_read_upstream_caps_each_output(self):
        """Each inlined output is capped at 25KB, and all of them share a budget with the input, so the
        state stays under 256KB; a larger one reaches the task as a _truncated marker (xcom reads it)."""
        items = self.rt['States']['Read_Upstream_Outputs']['Items']
        assert '25000' in items and '200000' in items
        output = self.rt['States']['Read_Upstream_Outputs']['ItemProcessor']['States']['Get_Dep_Output']['Output']
        assert '_truncated' in output and '$states.input.cap' in output

    def test_child_sfn_receives_upstream(self):
        """Run_Task_SFN passes upstream outputs to child SFN input."""
        inp = self.rt['States']['Run_Task_SFN']['Arguments']['Input']
        assert 'upstream' in inp

    def test_child_lambda_receives_upstream(self):
        """Run_Task_Lambda passes upstream outputs to child Lambda payload."""
        payload = self.rt['States']['Run_Task_Lambda']['Arguments']['Payload']
        assert 'upstream' in payload

    def test_upstream_only_passed_when_nonempty(self):
        """Upstream is only included when it has keys (avoids empty object noise)."""
        inp = self.rt['States']['Run_Task_SFN']['Arguments']['Input']
        assert '$count($keys($u)) > 0' in inp

    def test_prepare_task_input_computes_date_variables(self):
        """Prepare_Task_Input computes all jsonata-source variables from schema."""
        pti = self.rt['States']['Prepare_Task_Input']['Output']
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'sam', 'lambdas', 'console_api'))
        from task_variables import get_jsonata_vars
        for var in sorted(get_jsonata_vars()):
            assert var in pti, f"Missing variable in Prepare_Task_Input: {var} (defined in task_variables.py)"

    def test_prepare_task_input_backfill_overrides_computed(self):
        """Backfill variables override computed date variables (merge order: computed first, then user vars)."""
        pti = self.rt['States']['Prepare_Task_Input']['Output']
        assert '$merge([$dateVars, $vars])' in pti


# ============================================================
# Variable schema drift detection
# ============================================================

class TestVariableSchemaDrift:
    """Ensure run_task Prepare_Task_Input stays in sync with task_variables.py schema.

    Pre-v0.78, this also checked routes/backfill.py contained a Python
    builder mirroring the JSONata vars. After ADR #51 the bulk-backfill
    SFN passes only ``partition_key`` + ``current_date`` and the wrapper +
    run_task templates build all derived calendar vars in JSONata via
    ``Prepare_Task_Input``. The Python builder is gone, so the
    ``test_backfill_has_*`` tests are removed.
    """

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_template_generated_from_registry(self):
        """run_task $dateVars must match what the codegen produces from the registry.

        This is the generated-artifact check that replaces the old hand-sync drift
        tests: ``polyris/variables.py`` is the single source, the template is
        generated from it, so they cannot diverge. If this fails, run
        ``make generate-variables``.
        """
        from polyris.codegen.sync_variables import is_in_sync
        assert is_in_sync(), (
            "run_task Prepare_Task_Input $dateVars is out of sync with "
            "polyris/variables.py — run `make generate-variables`."
        )

    def test_schema_matches_registry(self):
        """The console_api TASK_VARIABLES schema is derived from the same registry."""
        from task_variables import TASK_VARIABLES
        from polyris.variables import VARIABLES
        assert set(TASK_VARIABLES) == set(VARIABLES)


class TestRestartStopsBothWrapperLevels:
    """§9, corrected: restarting a task must stop TWO separate executions, in
    a specific order — not just one.

    There are two levels: the OUTER dependency_wrapper (handles dependency
    waiting, then synchronously calls Run_Task -> the INNER run_task, via
    startExecution.sync:2) and the INNER run_task (the one with
    Wait_For_Decision/waiting_decision). An earlier fix attempt here killed
    only the inner run_task (reading 'run_task_helper_arn'), reasoning that
    'wrapper_execution_arn' — the field originally read — must be a typo,
    since it's never written anywhere in run_task's own template. That
    reasoning was incomplete: 'wrapper_execution_arn' IS genuinely written,
    by registration_helper's Save_Task_Record, for the OUTER wrapper
    specifically (Prepare_Inputs sets it to $states.context.Execution.Id,
    evaluated in the outer wrapper's own context) — a fact only visible by
    checking registration_helper's template too, not just run_task's.

    Killing only the inner run_task directly (while leaving the outer
    wrapper alive, synchronously waiting on it) caused a real, confirmed bug:
    the outer wrapper's Run_Task call sees its child forcibly aborted, which
    IS an internal error from the outer wrapper's own perspective — so its
    own Catch fires, routing to Handle_Failure -> failure_handler ->
    notify_dependents('failed'), immediately, well before the new attempt's
    own work even begins. This surfaced live: clicking Restart caused the
    downstream task to be marked upstream_failed within seconds, while the
    restarted task's own underlying work was still genuinely running (and
    later genuinely succeeded, ~30s later, per its own execution history) —
    a false failure notification racing ahead of the real outcome.

    The fix: kill the OUTER wrapper FIRST (external StopExecution does not
    trigger a wrapper's own internal Catch — that only fires for errors from
    WITHIN an execution, not from being stopped externally — so this cannot
    cascade a false notification), THEN the inner run_task second (safe now
    that the outer wrapper, which would otherwise catch this, is already
    dead) — preventing both the cascading false-failure bug and run_task
    being orphaned as a ghost."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('restart_task')

    def test_outer_wrapper_stopped_first_using_wrapper_execution_arn(self):
        state = self.rt['States']['Stop_Old_Outer_Wrapper']
        arn_expr = state['Arguments']['ExecutionArn']
        m = re.search(r'\$states\.input\.item\.(\w+)\.S', arn_expr)
        assert m, f"Could not find a field reference in: {arn_expr}"
        assert m.group(1) == 'wrapper_execution_arn'

    def test_inner_run_task_stopped_second_using_run_task_helper_arn(self):
        state = self.rt['States']['Stop_Old_Inner_Wrapper']
        arn_expr = state['Arguments']['ExecutionArn']
        m = re.search(r'\$states\.input\.item\.(\w+)\.S', arn_expr)
        assert m, f"Could not find a field reference in: {arn_expr}"
        assert m.group(1) == 'run_task_helper_arn'

    def test_outer_wrapper_chains_to_inner_before_delete(self):
        """The ORDER is the entire point of this fix — outer must be fully
        attempted (success or caught failure) before inner is touched, and
        inner must complete before Hand_Over_Ownership."""
        assert self.rt['States']['Check_Task_Exists']['Choices'][0]['Next'] == 'Stop_Old_Outer_Wrapper'
        outer = self.rt['States']['Stop_Old_Outer_Wrapper']
        assert outer['Next'] == 'Stop_Old_Inner_Wrapper'
        assert outer['Catch'][0]['Next'] == 'Stop_Old_Inner_Wrapper'
        inner = self.rt['States']['Stop_Old_Inner_Wrapper']
        assert inner['Next'] == 'Hand_Over_Ownership'
        assert inner['Catch'][0]['Next'] == 'Hand_Over_Ownership'

    def test_registration_helper_writes_wrapper_execution_arn_for_the_outer_wrapper(self):
        """Cross-template consistency: registration_helper's Save_Task_Record
        must actually persist wrapper_execution_arn, or Stop_Old_Outer_Wrapper
        has nothing to read. This is the check the earlier, incomplete fix
        attempt skipped — it only looked at run_task's own template."""
        registration = load('registration')
        item = registration['States']['Save_Task_Record']['Arguments']['Item']
        assert 'wrapper_execution_arn' in item

    def test_run_task_writes_run_task_helper_arn_for_the_inner_wrapper(self):
        """Cross-template consistency for the inner kill target."""
        run_task = load('run_task')
        state = run_task['States']['Claim']
        update_expr = state['Arguments']['UpdateExpression']
        assert 'run_task_helper_arn = :arn' in update_expr
        assert state['Arguments']['ExpressionAttributeValues'][':arn'] == {'S': '{% $states.context.Execution.Id %}'}


class TestRunTaskWritesOnlyUnderItsGeneration:
    """§9 layer 2 became the generation (ADR #137 invariant 5): each write of run_task is conditional on
    the status it expects and on the generation it was started with. Whatever the console did since
    (a raised generation), a stale write is rejected by DynamoDB itself, structurally, and run_task ends
    quietly. The census in tests/sdk/test_task_row_writers.py checks every write against the table."""

    STATUS_WRITING_STATES = ['Claim', 'Ask']

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    @pytest.mark.parametrize('state_name', STATUS_WRITING_STATES)
    def test_has_a_generation_fence(self, state_name):
        from polyris.coordination import task_row
        condition = self.rt['States'][state_name]['Arguments']['ConditionExpression']
        assert condition.endswith(f' AND {task_row.FENCE}')
        assert ':g' in self.rt['States'][state_name]['Arguments']['ExpressionAttributeValues']

    def test_a_refused_write_ends_quietly_with_no_further_side_effects(self):
        """A Succeed state — terminates the superseded execution immediately, before any event,
        notification or Slack message with superseded information."""
        catch = self.rt['States']['Ask']['Catch']
        assert catch[0]['ErrorEquals'] == ['DynamoDB.ConditionalCheckFailedException']
        assert catch[0]['Next'] == 'Superseded'
        state = self.rt['States']['Superseded']
        assert state['Type'] == 'Succeed'
        assert 'Next' not in state


class TestRestartTaskConfigAndOutletsThreading:
    """task_config/outlets are deploy-time DAG properties, never stored on
    the per-execution DynamoDB record. Start_New_Wrapper previously tried
    to reconstruct them from item.task_config.S/item.outlets.S — fields
    that never existed — always silently yielding {} and [] regardless of
    the task's real configuration. Fixed by having restart_task's Python
    handler look these up from the registry and pass them explicitly at
    the top level of restart_task_helper's input; Get_Task_Config must
    preserve them through to Start_New_Wrapper."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('restart_task')

    def test_get_task_config_preserves_task_config_and_outlets_from_input(self):
        output_expr = self.rt['States']['Get_Task_Config']['Output']
        assert "'task_config'" in output_expr
        assert "'outlets'" in output_expr
        assert '$states.input.task_config' in output_expr
        assert '$states.input.outlets' in output_expr

    def test_start_new_wrapper_reads_task_config_and_outlets_from_top_level_input(self):
        """Must read $states.input.task_config / $states.input.outlets
        directly (as threaded through by Get_Task_Config above) — NOT
        $states.input.item.task_config.S / item.outlets.S, which was the
        bug (those fields never existed on the stored item). Since restart
        replays starter_input (ADR #130 §6.5), the input is one expression;
        tests/sdk/test_restart_task_template.py evaluates it."""
        expr = self.rt['States']['Start_New_Wrapper']['Arguments']['Input']
        assert '$states.input.task_config' in expr
        assert '$states.input.outlets' in expr
        assert 'item.task_config' not in expr
        assert 'item.outlets' not in expr


class TestRestartNamePrefix:
    """A restarted attempt's underlying business SFN invocation (Run_Task_SFN
    — the only Run_Task_* variant that invokes another Step Function with a
    controllable Name; the others call AWS services directly and don't have
    an equivalent) gets a 'restart-' name prefix when attempt > 1. In 1.3 the
    claim counts the row's starts into $attempt."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_name_expression_checks_attempt_greater_than_one(self):
        name_expr = self.rt['States']['Run_Task_SFN']['Arguments']['Name']
        assert "$attempt > 1 ? 'restart-' : ''" in name_expr

    def test_first_attempt_has_no_restart_prefix_at_runtime(self):
        """Evaluate the conditional against representative attempts, not just its text."""
        import jsonata
        name_expr = self.rt['States']['Run_Task_SFN']['Arguments']['Name']
        names = []
        for attempt in (1, 2):
            j = jsonata.Jsonata(name_expr[2:-2].strip())
            j.assign('states', {'context': {'Execution': {'Input': {'execution_name': 'load-2026-11-18-x'}}}})
            j.assign('attempt', attempt)
            names.append(j.evaluate(None))
        assert names[0].startswith('load-2026-11-18-x-') and names[1].startswith('restart-load-2026-11-18-x-')
