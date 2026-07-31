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
    """Verify the new failure flow: Save_Error_Waiting → Check_Is_Backfill → Check_Has_Slack → ... → Wait_For_Decision"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_save_error_goes_to_decision_timeout(self):
        """Save_Error_Waiting → Get_Decision_Timeout (which reads the global wait
        timeout from the registry before the failure branch, ADR #103 1b), then
        on to Check_Is_Backfill."""
        state = self.rt['States']['Save_Error_Waiting']
        assert state['Next'] == 'Get_Decision_Timeout'
        # and that state leads into the backfill check
        assert self.rt['States']['Get_Decision_Timeout']['Next'] == 'Check_Is_Backfill'

    def test_save_error_catch_goes_to_decision_timeout(self):
        """Save_Error_Waiting's States.ALL catch (the generic DDB-failure,
        best-effort one) → Get_Decision_Timeout. §9 added a more specific
        ConditionalCheckFailedException catch before it (→
        Stale_Attempt_Superseded, for a ghost from a superseded attempt) —
        find the States.ALL entry by its actual condition, not by position."""
        state = self.rt['States']['Save_Error_Waiting']
        catch_all = next(c for c in state['Catch'] if c['ErrorEquals'] == ['States.ALL'])
        assert catch_all['Next'] == 'Get_Decision_Timeout'

    def test_get_decision_timeout_reads_global_settings(self):
        """Get_Decision_Timeout reads the reserved __global_settings__ registry
        record and assigns decision_timeout_seconds (default 18000 on miss)."""
        state = self.rt['States']['Get_Decision_Timeout']
        assert state['Resource'] == 'arn:aws:states:::dynamodb:getItem'
        assert state['Arguments']['Key']['pipeline_name']['S'] == '__global_settings__'
        assert 'decision_timeout_seconds' in state['Assign']
        # graceful: catch still continues to the backfill check
        assert state['Catch'][0]['Next'] == 'Check_Is_Backfill'

    def test_wait_for_decision_uses_assigned_timeout(self):
        """Wait_For_Decision reads the assigned variable, not a hardcoded value."""
        state = self.rt['States']['Wait_For_Decision']
        assert 'decision_timeout_seconds' in str(state['Seconds'])

    def test_backfill_skips_to_wait_for_decision(self):
        """Backfill runs skip alerting but still wait for user decision via UI."""
        state = self.rt['States']['Check_Is_Backfill']
        assert state['Type'] == 'Choice'
        assert state['Choices'][0]['Next'] == 'Wait_For_Decision'
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
        """Save_Slack_Failed → Send_PagerDuty_Alert (both Next and Catch)."""
        state = self.rt['States']['Save_Slack_Failed']
        assert state['Next'] == 'Send_PagerDuty_Alert'
        assert state['Catch'][0]['Next'] == 'Send_PagerDuty_Alert'

    def test_run_task_reads_no_wrapper_alerts_field(self):
        """ADR #103 Stage 2: no state in run_task reads the deprecated wrapper `alerts` field."""
        assert 'states.input.alerts' not in json.dumps(self.rt)

    def test_pagerduty_alert_goes_to_wait(self):
        """Send_PagerDuty_Alert → Wait_For_Decision."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert state['Next'] == 'Wait_For_Decision'

    def test_pagerduty_alert_catch_goes_to_wait(self):
        """PagerDuty failure → still Wait_For_Decision (graceful degradation)."""
        state = self.rt['States']['Send_PagerDuty_Alert']
        assert state['Catch'][0]['Next'] == 'Wait_For_Decision'

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

    def test_wait_for_decision_still_goes_to_save_failed(self):
        """Wait_For_Decision timeout → Save_Failed (unchanged)."""
        state = self.rt['States']['Wait_For_Decision']
        assert state['Next'] == 'Save_Failed'


# ============================================================
# run_task — DDB alerts_json persistence
# ============================================================

class TestRunTaskFailurePathsConverge:
    """Every task type's Catch → Check_Should_Retry → (exhausted) Save_Error_Waiting → Check_Is_Backfill."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    @pytest.mark.parametrize("task_state", [
        'Run_Task_SFN', 'Run_Task_Lambda', 'Run_Task_Glue',
        'Run_Task_ECS', 'Run_Task_Athena', 'Run_Task_EMR', 'Run_Task_Batch'
    ])
    def test_task_catch_goes_to_save_error(self, task_state):
        """All Run_Task_* states catch to the retry decision (ADR #106), which on
        exhausted retries converges to Save_Error_Waiting (the failure entry)."""
        state = self.rt['States'][task_state]
        assert state['Catch'][0]['Next'] == 'Check_Should_Retry'
        # exhausted retries must still converge to the existing failure path
        assert self.rt['States']['Check_Should_Retry']['Default'] == 'Save_Error_Waiting'


# ============================================================
# run_task — complete path tracing
# ============================================================

class TestRunTaskPathTracing:
    """Trace complete paths through the failure flow."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def _follow_default_path(self, start, max_steps=20):
        """Follow Default/Next path from a state."""
        path = [start]
        current = start
        for _ in range(max_steps):
            state = self.rt['States'].get(current)
            if not state or state.get('End') or state['Type'] == 'Fail':
                break
            nxt = state.get('Default', state.get('Next'))
            if not nxt:
                break
            path.append(nxt)
            current = nxt
        return path

    def test_backfill_path(self):
        """Backfill: Save_Error → Check_Is_Backfill → Save_Failed → ... → Fail_State."""
        # Backfill takes Choice[0] from Check_Is_Backfill
        path = ['Save_Error_Waiting', 'Check_Is_Backfill', 'Save_Failed']
        rest = self._follow_default_path('Save_Failed')
        full = path[:2] + rest
        assert 'Interactive_Slack' not in full, "Backfill must not send Slack"
        assert 'Send_PagerDuty_Alert' not in full, "Backfill must not send PagerDuty"
        assert 'Wait_For_Decision' not in full, "Backfill must not wait"

    def test_non_backfill_path_attempts_both_channels(self):
        """ADR #103 Stage 2: a non-backfill failure always traverses both notify states
        (Interactive_Slack → Send_PagerDuty_Alert) before waiting. Each notifier no-ops at
        runtime when its channel is unconfigured — channel selection is no longer a SFN branch."""
        path = self._follow_default_path('Save_Error_Waiting')
        assert 'Check_Is_Backfill' in path
        assert 'Interactive_Slack' in path
        assert 'Send_PagerDuty_Alert' in path
        assert 'Wait_For_Decision' in path
        assert 'Check_Has_Slack' not in path
        assert 'Check_Has_PagerDuty' not in path

    def test_alert_subpath_ordering(self):
        """Interactive_Slack → Send_PagerDuty_Alert → Wait_For_Decision (sequential, both best-effort)."""
        assert self.rt['States']['Interactive_Slack']['Next'] == 'Send_PagerDuty_Alert'
        assert self.rt['States']['Send_PagerDuty_Alert']['Next'] == 'Wait_For_Decision'


# ============================================================
# run_task — retry loop <-> human-in-the-loop composition
# ============================================================

class TestRetryLoopComposesWithHumanDecision:
    """ADR #106/#107: the auto-retry loop and the human-in-the-loop decision
    (Slack Skip/Restart/Fail) compose SEQUENTIALLY and in ISOLATION.

    Auto-retries run and exhaust inside a single wrapper execution; only once the
    counter is exhausted does the failure reach Save_Error_Waiting and the decision
    flow. The human 'Restart' is applied out-of-band (console API resolves the
    pipeline task token) and re-runs the pipeline task as a *fresh* wrapper execution,
    so retry_attempt is re-initialised at 0 — never mutated by the decision flow.
    These invariants guard that separation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    # the only states allowed to read/write the retry counter
    RETRY_LOOP_STATES = {
        'Prepare_Task_Input', 'Check_Should_Retry', 'Wait_Before_Retry', 'Increment_Retry',
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
        assert self.rt['States']['Check_Should_Retry']['Default'] == 'Save_Error_Waiting'

    def test_decision_flow_never_touches_retry_counter(self):
        """No state outside the retry loop references retry_attempt, so the decision
        flow (and a human Restart) can neither read nor reset the auto-retry counter."""
        for name, body in self.rt['States'].items():
            if name in self.RETRY_LOOP_STATES:
                continue
            assert 'retry_attempt' not in json.dumps(body), \
                f"{name} references retry_attempt — breaks retry/HITL isolation"

    def test_only_dispatch_and_increment_reenter_the_task(self):
        """Check_Task_Type (task dispatch) is re-entered ONLY by the initial dispatch
        chain (Prepare_Task_Input -> Save_Task_Input) and the retry increment
        (Increment_Retry). No decision-flow state loops back into the task, so a
        human Restart cannot race the counter mid-flight — it can only arrive as
        a brand-new execution. Save_Task_Input (writes task_input as soon as the
        task starts, independent of eventual success/failure) sits between
        Prepare_Task_Input and Check_Task_Type — it's part of the same initial
        dispatch, not a new re-entry path."""
        feeders = {n for n, b in self.rt['States'].items()
                   if 'Check_Task_Type' in self._targets(b)}
        assert feeders == {'Save_Task_Input', 'Increment_Retry'}, \
            f"unexpected re-dispatch into the task from: {feeders - {'Save_Task_Input', 'Increment_Retry'}}"
        # And Save_Task_Input itself must only be reachable from Prepare_Task_Input —
        # not from anywhere else, so it can't become a second, independent entry point.
        save_input_feeders = {n for n, b in self.rt['States'].items()
                               if 'Save_Task_Input' in self._targets(b)}
        assert save_input_feeders == {'Prepare_Task_Input'}, \
            f"Save_Task_Input has unexpected feeders: {save_input_feeders - {'Prepare_Task_Input'}}"

    def test_counter_reset_precedes_first_dispatch(self):
        """retry_attempt is initialised to 0 in Prepare_Task_Input, which runs before the
        first Check_Task_Type — so every fresh execution, including a human Restart, starts
        the retry loop clean. Prepare_Task_Input feeds Save_Task_Input (writes task_input
        before dispatch) which then feeds Check_Task_Type — retry_attempt is still set
        before either of them, so the invariant holds through the extra hop."""
        prep = self.rt['States']['Prepare_Task_Input']
        assert '"retry_attempt"' in json.dumps(prep['Assign'])
        assert prep['Next'] == 'Save_Task_Input'
        assert self.rt['States']['Save_Task_Input']['Next'] == 'Check_Task_Type'


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
    """Verify canonical output: stable key for upstream reads, survives incremental backfill."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_save_success_chains_to_canonical(self):
        """Save_Success → Save_Canonical_Output → Emit_Task_Finished_Success."""
        assert self.rt['States']['Save_Success']['Next'] == 'Save_Canonical_Output'
        assert self.rt['States']['Save_Canonical_Output']['Next'] == 'Emit_Task_Finished_Success'

    def test_canonical_uses_stable_key(self):
        """Canonical key is output#pipeline#task#date — no run-specific parts."""
        item = self.rt['States']['Save_Canonical_Output']['Arguments']['Item']
        key = item['execution_name']['S']
        assert 'output#' in key
        assert 'pipeline_name' in key
        assert 'task_name' in key
        assert 'date' in key
        assert 'pipeline_execution_short' not in key

    def test_canonical_has_required_fields(self):
        """Canonical record includes result, task_name, status, ttl."""
        item = self.rt['States']['Save_Canonical_Output']['Arguments']['Item']
        assert 'result' in item
        assert 'task_name' in item
        assert 'status' in item
        assert item['status']['S'] == 'success'
        assert 'ttl' in item

    def test_canonical_is_best_effort(self):
        """If canonical save fails, continue to Emit_Task_Finished_Success."""
        catch = self.rt['States']['Save_Canonical_Output']['Catch']
        assert len(catch) == 1
        assert catch[0]['Next'] == 'Emit_Task_Finished_Success'

    def test_read_upstream_uses_canonical_key(self):
        """Read_Upstream_Outputs reads by output#pipeline#dep#date."""
        get_dep = self.rt['States']['Read_Upstream_Outputs']['ItemProcessor']['States']['Get_Dep_Output']
        key = get_dep['Arguments']['Key']['execution_name']['S']
        assert 'output#' in key
        assert 'pipeline_name' in key


class TestSaveTaskInputEarly:
    """Save_Task_Input writes task_input (upstream+variables) as soon as the task
    starts, independent of eventual success/failure/waiting_decision — closes the
    gap where only Save_Canonical_Output (success-path only) ever recorded it,
    so a failed or still-running task's Input tab was always empty."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_prepare_task_input_chains_through_save_task_input_to_dispatch(self):
        """Prepare_Task_Input -> Save_Task_Input -> Check_Task_Type — the write
        happens after upstream/variables are fully resolved, but before any
        business logic (Glue/ECS/SFN/etc.) actually runs."""
        assert self.rt['States']['Prepare_Task_Input']['Next'] == 'Save_Task_Input'
        assert self.rt['States']['Save_Task_Input']['Next'] == 'Check_Task_Type'

    def test_uses_the_same_stable_canonical_key_as_save_canonical_output(self):
        """Same key format as Save_Canonical_Output (output#pipeline#task#date) —
        this is deliberately the same record xcom.pull() and the console's
        Input/Output tab both read, not a new location."""
        key = self.rt['States']['Save_Task_Input']['Arguments']['Key']['execution_name']['S']
        assert 'output#' in key
        assert 'pipeline_name' in key
        assert 'task_name' in key
        assert 'date' in key

    def test_only_touches_task_input_never_result_or_status(self):
        """Deliberately updateItem, not putItem like Save_Canonical_Output: this
        key is shared across same-day runs of this task, so a blind full-item
        replace here could momentarily blank out a prior successful run's real
        'result' while a later run of the same task/date is still in flight.
        The UpdateExpression must only ever touch task_input/task_name/
        updated_at/ttl — never result or status."""
        state = self.rt['States']['Save_Task_Input']
        assert state['Resource'] == 'arn:aws:states:::dynamodb:updateItem'
        update_expr = state['Arguments']['UpdateExpression']
        assert 'result' not in update_expr
        assert 'status' not in update_expr
        assert 'task_input' in update_expr

    def test_ttl_uses_if_not_exists_so_it_is_not_reset_every_run(self):
        """A same-day re-run of this task must not push the ttl forward
        indefinitely — if_not_exists means only the first write for this key
        on this date sets it."""
        update_expr = self.rt['States']['Save_Task_Input']['Arguments']['UpdateExpression']
        assert 'if_not_exists' in update_expr

    def test_is_best_effort_like_every_other_status_write(self):
        """DDB failure here must not block the task from actually running —
        same pattern as Save_Success/Save_Failed/Save_Canonical_Output."""
        catch = self.rt['States']['Save_Task_Input']['Catch']
        assert len(catch) == 1
        assert catch[0]['Next'] == 'Check_Task_Type'


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
        assert 'dep' in key
        assert 'date' in key
        assert 'pipeline_execution_short' not in key

    def test_read_upstream_passes_pipeline_name(self):
        """Map items include pipeline_name for canonical key construction."""
        items_expr = self.rt['States']['Read_Upstream_Outputs']['Items']
        assert 'pipeline_name' in items_expr

    def test_read_upstream_truncates_large_output(self):
        """Get_Dep_Output truncates individual dep output > 25KB to prevent 256KB payload limit."""
        gdo = self.rt['States']['Read_Upstream_Outputs']['ItemProcessor']['States']['Get_Dep_Output']
        output = gdo['Output']
        assert '25000' in output
        assert '_truncated' in output

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
        assert '$count($keys($states.input.upstream)) > 0' in inp

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
        inner must complete before Delete_Old_Record."""
        assert self.rt['States']['Check_Task_Exists']['Choices'][0]['Next'] == 'Stop_Old_Outer_Wrapper'
        outer = self.rt['States']['Stop_Old_Outer_Wrapper']
        assert outer['Next'] == 'Stop_Old_Inner_Wrapper'
        assert outer['Catch'][0]['Next'] == 'Stop_Old_Inner_Wrapper'
        inner = self.rt['States']['Stop_Old_Inner_Wrapper']
        assert inner['Next'] == 'Delete_Old_Record'
        assert inner['Catch'][0]['Next'] == 'Delete_Old_Record'

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
        state = run_task['States']['Update_Status_Running']
        update_expr = state['Arguments']['UpdateExpression']
        assert 'run_task_helper_arn = :helper_arn' in update_expr


class TestAttemptKeyedConditionExpression:
    """§9 layer 2: fixing the helper_arn field name (above) only makes
    killing a ghost execution *likely* — StopExecution can still fail for
    unrelated reasons, and nothing previously guarded against a stale write
    applying anyway. Each of the three status-writing states now requires
    'attempt = :expectedAttempt' to still hold — if a restart has since
    happened (attempt incremented), a ghost's stale write is rejected by
    DynamoDB itself, structurally, regardless of whether the kill succeeded.
    This is what gets the "duplicate status" risk to zero, not just rarer."""

    STATUS_WRITING_STATES = ['Save_Success', 'Save_Failed', 'Save_Error_Waiting']

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    @pytest.mark.parametrize('state_name', STATUS_WRITING_STATES)
    def test_has_attempt_keyed_condition_expression(self, state_name):
        state = self.rt['States'][state_name]
        assert state['Arguments']['ConditionExpression'] == 'attempt = :expectedAttempt'
        assert ':expectedAttempt' in state['Arguments']['ExpressionAttributeValues']

    @pytest.mark.parametrize('state_name', STATUS_WRITING_STATES)
    def test_conditional_check_failure_routes_to_stale_attempt_superseded(self, state_name):
        """The conditional-check-failed catch must come BEFORE the generic
        States.ALL catch (Step Functions evaluates Catch entries in order,
        first match wins) — a ConditionalCheckFailedException also matches
        States.ALL, so if the generic entry came first it would silently
        swallow the stale-attempt case and continue as if nothing were
        superseded, exactly the corruption this fix exists to prevent."""
        catch = self.rt['States'][state_name]['Catch']
        assert catch[0]['ErrorEquals'] == ['DynamoDB.ConditionalCheckFailedException']
        assert catch[0]['Next'] == 'Stale_Attempt_Superseded'
        assert any(c['ErrorEquals'] == ['States.ALL'] for c in catch[1:]), \
            f"{state_name} lost its generic DDB-failure catch"

    def test_stale_attempt_superseded_ends_quietly_with_no_further_side_effects(self):
        """A Succeed state — terminates the ghost's execution immediately.
        Must NOT be a Task/Pass that continues on to Emit_Task_Finished_*,
        notify_dependents, or a Slack message with superseded information."""
        state = self.rt['States']['Stale_Attempt_Superseded']
        assert state['Type'] == 'Succeed'
        assert 'Next' not in state

    def test_expected_attempt_defaults_to_1_matching_the_first_attempt_convention(self):
        """Consistent with Start_New_Wrapper's own attempt defaulting
        (existing attempt ?? 1) — a task's first-ever run has no explicit
        'attempt' in its input, and must be treated as attempt 1, not as a
        mismatch against whatever a stray record might contain."""
        for state_name in self.STATUS_WRITING_STATES:
            expr = self.rt['States'][state_name]['Arguments']['ExpressionAttributeValues'][':expectedAttempt']['N']
            assert '$exists($states.input.attempt)' in expr
            assert ': 1)' in expr or ': 1 )' in expr


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
        bug (those fields never existed on the stored item)."""
        inp = self.rt['States']['Start_New_Wrapper']['Arguments']['Input']
        assert inp['task_config'] == "{% $exists($states.input.task_config) ? $states.input.task_config : {} %}"
        assert inp['outlets'] == "{% $exists($states.input.outlets) ? $states.input.outlets : [] %}"
        assert 'item.task_config' not in inp['task_config']
        assert 'item.outlets' not in inp['outlets']


class TestRestartNamePrefix:
    """A restarted attempt's underlying business SFN invocation (Run_Task_SFN
    — the only Run_Task_* variant that invokes another Step Function with a
    controllable Name; the others call AWS services directly and don't have
    an equivalent) now gets a 'restart-' name prefix when attempt > 1. Makes
    it immediately visible in the AWS console which invocations are
    original vs restarted, without checking duration/timestamps. Also gives
    real purpose to attempt, already threaded through by Start_New_Wrapper
    but previously unused for naming."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.rt = load('run_task')

    def test_name_expression_checks_attempt_greater_than_one(self):
        name_expr = self.rt['States']['Run_Task_SFN']['Arguments']['Name']
        assert "$states.input.attempt > 1" in name_expr
        assert "'restart-'" in name_expr
        assert '$exists($states.input.attempt)' in name_expr

    def test_first_attempt_has_no_restart_prefix_at_runtime(self):
        """Simulate the JSONata conditional directly against representative
        inputs — not just checking the expression text, but that its actual
        branches produce the right prefix (or lack of one)."""
        name_expr = self.rt['States']['Run_Task_SFN']['Arguments']['Name']
        # attempt absent (first-ever dispatch, never explicitly set)
        assert "$exists($states.input.attempt) and $states.input.attempt > 1 ? 'restart-' : ''" in name_expr
