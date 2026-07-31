"""Generator-engine tests — ASL validation + the public generation surface.

Two clusters, both driving real code paths (CLAUDE.md #13):

  1. ``validate_asl`` — the Amazon States Language validator. Exercised with
     hand-built ASL dicts covering every structural rule it enforces
     (missing fields, bad transitions, Choice/Parallel/Map shape,
     reachability, terminal states).

  2. The public generation functions (``generate_step_function_json``,
     ``generate_dag_json``, ``generate_mermaid``, asset/EventBridge JSON,
     debug info, hashing) — driven from real DAGs built with the public DSL.
     A key property test asserts that the machine we *generate* is itself
     *valid* under ``validate_asl``.
"""
from __future__ import annotations

import json

from polyris.generators import (
    validate_asl,
    generate_step_function_json,
    generate_dag_json,
    generate_dag_hash,
    generate_mermaid,
    generate_debug_info,
    generate_eventbridge_schedule,
    generate_assets_json,
    generate_all_assets,
    _build_dag_visualization_nodes_edges,
)

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


def _asl(states, start="A"):
    return {"StartAt": start, "States": states}


# ============================================================ #
# validate_asl
# ============================================================ #
class TestValidateAsl:
    def test_valid_minimal_machine(self):
        ok, errors, warnings = validate_asl(_asl({"A": {"Type": "Pass", "End": True}}))
        assert ok is True
        assert errors == []
        assert warnings == []

    def test_missing_start_at_errors(self):
        ok, errors, _ = validate_asl({"States": {"A": {"Type": "Pass", "End": True}}})
        assert ok is False
        assert any("StartAt" in e for e in errors)

    def test_missing_states_short_circuits(self):
        ok, errors, warnings = validate_asl({"StartAt": "A"})
        assert ok is False
        assert any("States" in e for e in errors)
        assert warnings == []  # returns immediately, no further checks

    def test_start_at_nonexistent_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"Type": "Pass", "End": True}}, start="X"))
        assert ok is False
        assert any("does not exist" in e for e in errors)

    def test_state_missing_type_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"End": True}}))
        assert ok is False
        assert any("Missing required field 'Type'" in e for e in errors)

    def test_invalid_state_type_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"Type": "Nonsense", "End": True}}))
        assert ok is False
        assert any("Invalid Type" in e for e in errors)

    def test_succeed_with_next_warns(self):
        ok, _errors, warnings = validate_asl(_asl({
            "A": {"Type": "Succeed", "Next": "B"},
            "B": {"Type": "Pass", "End": True},
        }))
        assert ok is True  # only a warning, not an error
        assert any("should not have 'Next'" in w for w in warnings)

    def test_choice_without_choices_errors(self):
        ok, errors, _ = validate_asl(_asl({
            "A": {"Type": "Choice", "Default": "B"},
            "B": {"Type": "Pass", "End": True},
        }))
        assert ok is False
        assert any("missing 'Choices'" in e for e in errors)

    def test_choice_branch_without_next_errors(self):
        ok, errors, _ = validate_asl(_asl({
            "A": {"Type": "Choice", "Choices": [{"Variable": "x"}], "Default": "B"},
            "B": {"Type": "Pass", "End": True},
        }))
        assert ok is False
        assert any("Choice[0] missing 'Next'" in e for e in errors)

    def test_choice_without_default_warns(self):
        ok, _errors, warnings = validate_asl(_asl({
            "A": {"Type": "Choice", "Choices": [{"Next": "B"}]},
            "B": {"Type": "Pass", "End": True},
        }))
        assert ok is True
        assert any("Choice without 'Default'" in w for w in warnings)

    def test_choice_with_default_no_default_warning(self):
        ok, _errors, warnings = validate_asl(_asl({
            "A": {"Type": "Choice", "Choices": [{"Next": "B"}], "Default": "B"},
            "B": {"Type": "Pass", "End": True},
        }))
        assert ok is True
        assert not any("Choice without 'Default'" in w for w in warnings)

    def test_state_without_end_or_next_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"Type": "Task"}}))
        assert ok is False
        assert any("Must have either 'End' or 'Next'" in e for e in errors)

    def test_both_end_and_next_warns(self):
        ok, _errors, warnings = validate_asl(_asl({
            "A": {"Type": "Task", "End": True, "Next": "B"},
            "B": {"Type": "Pass", "End": True},
        }))
        assert ok is True
        assert any("Has both 'End' and 'Next'" in w for w in warnings)

    def test_next_to_nonexistent_state_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"Type": "Task", "Next": "Z"}}))
        assert ok is False
        assert any("non-existent state 'Z'" in e for e in errors)

    def test_unreachable_state_warns(self):
        ok, _errors, warnings = validate_asl(_asl({
            "A": {"Type": "Pass", "End": True},
            "B": {"Type": "Pass", "End": True},  # never referenced
        }))
        assert ok is True
        assert any("unreachable" in w for w in warnings)

    def test_no_terminal_states_warns(self):
        # A Choice that only loops back to itself — reachable, valid, but no
        # End/Succeed/Fail anywhere.
        ok, _errors, warnings = validate_asl(_asl({
            "A": {"Type": "Choice", "Choices": [{"Next": "A"}], "Default": "A"},
        }))
        assert ok is True
        assert any("No terminal states" in w for w in warnings)

    def test_parallel_without_branches_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"Type": "Parallel", "End": True}}))
        assert ok is False
        assert any("no branches" in e for e in errors)

    def test_parallel_branch_errors_bubble_up(self):
        ok, errors, _ = validate_asl(_asl({
            "A": {
                "Type": "Parallel", "End": True,
                "Branches": [{"StartAt": "X", "States": {"X": {"End": True}}}],  # X missing Type
            },
        }))
        assert ok is False
        assert any("Branch[0]" in e for e in errors)

    def test_map_without_iterator_errors(self):
        ok, errors, _ = validate_asl(_asl({"A": {"Type": "Map", "End": True}}))
        assert ok is False
        assert any("missing 'Iterator' or 'ItemProcessor'" in e for e in errors)

    def test_map_with_item_processor_validates(self):
        ok, errors, _ = validate_asl(_asl({
            "A": {
                "Type": "Map", "End": True,
                "ItemProcessor": {"StartAt": "X", "States": {"X": {"Type": "Pass", "End": True}}},
            },
        }))
        assert ok is True
        assert errors == []


# ============================================================ #
# public generation surface (driven from real DAGs)
# ============================================================ #
def _chain_dag():
    from polyris import DAG, task

    with DAG("gen_chain", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def extract():
            pass

        @task.sfn(arn=ARN)
        def load():
            pass

        extract() >> load()
    return dag


def _scheduled_dag():
    from polyris import DAG, task

    with DAG("gen_sched", schedule="rate(1 hour)") as dag:
        @task.sfn(arn=ARN)
        def run():
            pass
        run()
    return dag


def _asset_dag():
    from polyris import DAG, task, Asset, Column, types as t

    orders = Asset(name="shop/orders", schema=[Column("id", t.bigint()), Column("ts", t.date())])
    with DAG("gen_assets", schedule=None) as dag:
        @task.sfn(arn=ARN, outlets=[orders])
        def make():
            pass
        make()
    return dag


def _asset_triggered_dag():
    """A DAG with both a triggering (schedule=) and a produced (outlets=)
    asset, and a plain scheduled sibling task — exercises Register_Pipeline,
    Register_Asset_Subscriptions, and Emit_Asset_Events in one generation,
    the three states most likely to reference a DynamoDB table by name."""
    from polyris import DAG, task, Asset

    trigger_asset = Asset("ns/trigger")
    output_asset = Asset("ns/output")
    with DAG("gen_asset_triggered", schedule=trigger_asset) as dag:
        @task.sfn(arn=ARN, outlets=[output_asset])
        def produce():
            pass
        produce()
    return dag


class TestNoUnsubstitutedTemplateVariables:
    """Guard against the class of bug where generators.py hardcodes a new
    '${some_table}'-style placeholder in a state definition without also
    adding it as a real parameter to generate_step_function_json — the
    placeholder then has no substitution mechanism anywhere in polyris-deploy's
    per-pipeline CloudFormation flow (no DefinitionSubstitutions there, unlike
    the platform's helper SFN templates) and gets deployed to AWS verbatim as
    the literal string, which then fails at the first real API call that uses
    it (confirmed in practice: DynamoDB rejected TableName='${asset_subscriptions_table}'
    with a ValidationException). Every known substitutable variable must be
    fully resolved — zero '${' left anywhere in the output — once a complete,
    realistic parameter set is supplied."""

    def test_full_parameter_set_leaves_no_placeholder_in_output(self):
        for dag in (_chain_dag(), _scheduled_dag(), _asset_dag(), _asset_triggered_dag()):
            asl_json = generate_step_function_json(
                dag,
                wrapper_arn="arn:aws:states:us-east-1:123456789012:stateMachine:wrapper",
                registry_table="real-registry-table",
                tokens_table="real-tokens-table",
                asset_subscriptions_table="real-asset-subscriptions-table",
            )
            assert "${" not in asl_json, (
                f"Unsubstituted '${{...}}' placeholder found in generated ASL for "
                f"DAG '{dag.dag_id}' despite a complete parameter set — a new "
                f"template variable was added to generators.py without threading "
                f"it through generate_step_function_json's parameters (and, in "
                f"deploy.py, an SSM read + call-site argument). Offending JSON:\n"
                f"{asl_json}"
            )

    def test_omitted_parameters_do_fall_back_to_placeholders(self):
        """Control: confirms the assertion above is meaningful — without a
        parameter set, placeholders ARE present, so the previous test's
        'no ${{' check is actually exercising the substitution, not vacuously
        passing because nothing ever produces '${' in the first place."""
        asl_json = generate_step_function_json(_asset_triggered_dag())
        assert "${" in asl_json


class TestPublicGeneration:
    def test_generated_machine_is_valid_asl(self):
        # The generator must emit a machine that passes our own ASL validator.
        asl = json.loads(generate_step_function_json(_chain_dag()))
        ok, errors, _ = validate_asl(asl)
        assert ok is True, errors

    def test_generate_dag_json_includes_dag_id(self):
        result = generate_dag_json(_chain_dag())
        assert isinstance(result, dict)
        assert "gen_chain" in json.dumps(result)

    def test_generate_dag_json_nodes_include_task_type(self):
        """The frontend reads node.task_type to render the task-type badge
        (SFN/Lambda/Glue/etc.) on the graph — including in 'current structure'
        (blueprint) mode, where there's no execution data to source it from
        otherwise. Without this, blueprint nodes show a bare box with no type
        information at all."""
        result = generate_dag_json(_chain_dag())
        assert all(n.get("task_type") == "sfn" for n in result["nodes"])

    def test_register_pipeline_dag_snapshot_includes_task_type(self):
        """Register_Pipeline's stored 'dag' field is what /pipeline-dag's
        registry fallback returns for 'current structure' (blueprint) mode.
        Must carry task_type or blueprint nodes render with no type badge."""
        asl = json.loads(generate_step_function_json(_chain_dag()))
        item = asl["States"]["Register_Pipeline"]["Arguments"]["Item"]
        dag = json.loads(item["dag"]["S"])
        assert all(n.get("task_type") == "sfn" for n in dag["nodes"])

    def test_generate_dag_hash_is_deterministic(self):
        h1 = generate_dag_hash(_chain_dag())
        h2 = generate_dag_hash(_chain_dag())
        assert isinstance(h1, str) and h1
        assert h1 == h2

    def test_generate_mermaid_renders_graph(self):
        out = generate_mermaid(_chain_dag())
        assert isinstance(out, str)
        assert "graph" in out.lower()

    def test_generate_debug_info_returns_structure(self):
        info = generate_debug_info(_chain_dag())
        assert isinstance(info, dict)
        assert info  # non-empty

    def test_generate_eventbridge_schedule_for_scheduled_dag(self):
        sched = generate_eventbridge_schedule(_scheduled_dag())
        assert isinstance(sched, dict)
        assert "rate(1 hour)" in json.dumps(sched)

    def test_generate_assets_json_contains_asset(self):
        out = generate_assets_json(_asset_dag())
        assert "shop/orders" in out

    def test_generate_all_assets_aggregates(self):
        result = generate_all_assets([_asset_dag()])
        assert isinstance(result, dict)
        assert "shop/orders" in json.dumps(result)


class TestDagVisualizationNodesEdgesUnified:
    """generate_dag_json (the public polyris-output/polyris-validate surface)
    and _build_pipeline_metadata (whose dag_metadata is stored in the pipeline
    registry and per-execution snapshots — what /pipeline-dag's registry
    fallback returns for 'current structure' mode) used to build their own,
    independent (nodes, edges) — and had quietly drifted: generate_dag_json
    had type/trigger_rule/tracked-steps that _build_pipeline_metadata's
    version lacked; _build_pipeline_metadata had wait_for (asset pull
    dependency) that generate_dag_json lacked. Both now call
    _build_dag_visualization_nodes_edges — one source of truth, full field
    set for both consumers, not whichever subset each caller happened to
    accumulate over time."""

    def test_both_callers_produce_identical_nodes_and_edges(self):
        """The core regression guard: if this ever drifts again, it fails
        here — not silently, three years from now, as two subtly different
        graphs depending which endpoint you hit."""
        dag = _chain_dag()
        from_public = generate_dag_json(dag)
        asl = json.loads(generate_step_function_json(dag))
        registry_dag = json.loads(asl["States"]["Register_Pipeline"]["Arguments"]["Item"]["dag"]["S"])
        assert from_public["nodes"] == registry_dag["nodes"]
        assert from_public["edges"] == registry_dag["edges"]

    def test_includes_type_marker_and_trigger_rule(self):
        """Both fields used to be generate_dag_json-only — trigger_rule was
        entirely absent from the registry/blueprint-mode data before this
        unification, so a non-default trigger_rule never showed on a
        current-structure node."""
        from polyris import DAG, task
        with DAG("d") as dag:
            @task.sfn(arn=ARN)
            def a():
                pass
            @task.sfn(arn=ARN, trigger_rule="all_done")
            def b():
                pass
            b(a())
        nodes, _ = _build_dag_visualization_nodes_edges(dag)
        by_id = {n["id"]: n for n in nodes}
        assert by_id["a"]["type"] == "task"
        assert "trigger_rule" not in by_id["a"]  # default, not surfaced
        assert by_id["b"]["trigger_rule"] == "all_done"

    def test_includes_wait_for(self):
        """wait_for used to be _build_pipeline_metadata-only — absent from
        generate_dag_json (polyris-output --json) before this unification."""
        from polyris import DAG, task, Asset
        upstream = Asset("ns/upstream")
        with DAG("d") as dag:
            @task.sfn(arn=ARN, wait_for=[upstream.within(hours=24)])
            def consumer():
                pass
            consumer()
        nodes, _ = _build_dag_visualization_nodes_edges(dag)
        assert nodes[0]["wait_for"][0]["name"] == "ns/upstream"

    def test_includes_tracked_steps_glue_ecs_athena(self):
        """Direct Glue/ECS/Athena steps (not through @task) used to be
        entirely absent from _build_pipeline_metadata's dag_metadata — a
        pipeline using a direct step would show an incomplete graph in
        'current structure' mode, missing that step's node altogether."""
        from polyris import DAG, task, GlueTask
        with DAG("d") as dag:
            @task.sfn(arn=ARN)
            def prepare():
                pass
            etl = GlueTask(step_id="run_etl", job_name="etl")
            prepare() >> etl
        nodes, edges = _build_dag_visualization_nodes_edges(dag)
        glue_nodes = [n for n in nodes if n.get("type") == "glue"]
        assert len(glue_nodes) == 1
        assert any(e["from"] == "prepare" for e in edges)

    def test_excludes_infrastructure_steps(self):
        """Wait/Pass/SNS/etc. direct steps are not tasks a user wants to see
        as a graph node — only Glue/ECS/Athena are tracked."""
        from polyris import DAG
        from polyris.steps import Succeed
        with DAG("d") as dag:
            Succeed(step_id="done")
        nodes, _ = _build_dag_visualization_nodes_edges(dag)
        assert nodes == []


class TestDefineInputsPreservesControlFields:
    """Regression tests for a real bug: Define_Inputs' Output (added whenever
    a DAG declares `variables=`) used to REPLACE the whole state input rather
    than extend it, silently dropping three fields any *caller* of the
    execution may have set on the initial input:

      - register_only: used by polyris-register / deploy-time auto-registration
        to register metadata without running the pipeline. Dropped -> the
        Check_Register_Only Choice always saw it as absent and fell through to
        Default: Run_All_Tasks, meaning a "register only" invocation on any
        DAG with variables actually ran the real pipeline.
      - _suppress_asset_event / cascade_all: backfill isolation/cascade
        controls (ADR #57, set by bulk_backfill's cascade='none'/'all').
        Dropped -> a `cascade='none'` isolated backfill on a DAG with
        variables would silently NOT suppress downstream asset-triggered
        consumers, defeating the whole point of an isolated backfill.

    Only DAGs with `variables=` were affected; without variables, Define_Inputs
    isn't in the chain at all and $states.input passes through registration
    untouched (see the other _build_registration_chain states' explicit
    Output: JSONATA_PASS_INPUT).
    """

    def _dag_with_variables(self):
        from polyris import DAG, task

        with DAG("define-inputs-fields", variables={"env": "'prod'"}) as dag:
            @task.sfn(arn=ARN)
            def extract():
                pass
            extract()
        return dag

    def test_register_only_survives_define_inputs(self):
        asl = json.loads(generate_step_function_json(self._dag_with_variables()))
        output = asl["States"]["Define_Inputs"]["Output"]
        assert "register_only" in output
        assert output["register_only"] == (
            "{% $exists($states.input.register_only) ? "
            "$states.input.register_only : false %}"
        )

    def test_suppress_asset_event_survives_define_inputs(self):
        asl = json.loads(generate_step_function_json(self._dag_with_variables()))
        output = asl["States"]["Define_Inputs"]["Output"]
        assert "_suppress_asset_event" in output
        assert output["_suppress_asset_event"] == (
            "{% $exists($states.input._suppress_asset_event) ? "
            "$states.input._suppress_asset_event : false %}"
        )

    def test_cascade_all_survives_define_inputs(self):
        asl = json.loads(generate_step_function_json(self._dag_with_variables()))
        output = asl["States"]["Define_Inputs"]["Output"]
        assert "cascade_all" in output
        assert output["cascade_all"] == (
            "{% $exists($states.input.cascade_all) ? "
            "$states.input.cascade_all : false %}"
        )

    def test_check_register_only_reads_the_same_field_define_inputs_writes(self):
        """End-to-end wiring check: the field name Define_Inputs writes must be
        exactly the field name Check_Register_Only's Choice condition reads —
        a typo'd key name on either side would silently reintroduce the bug
        without any error, since JSONata property access on a mismatched key
        just evaluates to undefined/falsy rather than raising."""
        asl = json.loads(generate_step_function_json(self._dag_with_variables()))
        output = asl["States"]["Define_Inputs"]["Output"]
        condition = asl["States"]["Check_Register_Only"]["Choices"][0]["Condition"]
        assert "register_only" in output
        assert "$states.input.register_only" in condition
