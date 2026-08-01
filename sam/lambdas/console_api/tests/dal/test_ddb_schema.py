"""Pin DDB schema constants per CLAUDE.md #13.

These tests don't verify the constants do anything — they pin their VALUES
so a rename in `ddb_schema.py` requires deliberate test update (and surfaces
to reviewers what was actually changed).

If somebody changes `PipelineTokens.STATUS = 'pipeline_status'` thinking it's
harmless, this test fails loudly. That's the point.
"""

from dal.ddb_schema import PipelineTokens, TaskStatus


class TestPipelineTokensFields:
    """The exact field names on the pipeline-tokens DDB table.

    These values must match:
    - SAM template (`sam/template.yaml::PipelineTokensTable.AttributeDefinitions`)
    - SFN templates (e.g., `sam/sfn_templates/dependency_wrapper/sfn.tpl.json`)
    - All `routes/*.py` handlers that read or write these fields
    """

    def test_primary_key(self):
        assert PipelineTokens.EXECUTION_NAME == 'execution_name'

    def test_gsi_keys(self):
        assert PipelineTokens.BACKFILL_ID == 'backfill_id'
        assert PipelineTokens.PIPELINE_EXECUTION == 'pipeline_execution'
        assert PipelineTokens.PARENT_EXECUTION_ID == 'parent_execution_id'
        assert PipelineTokens.DATE == 'date'
        assert PipelineTokens.PIPELINE_NAME == 'pipeline_name'

    def test_status_field_is_not_pipeline_status(self):
        """Regression: v0.78 audit found `Attr('pipeline_status').eq('success')`
        which was a silent stub-like bug — DDB never returns matches because
        the field on the table is 'status', not 'pipeline_status'. Pin it."""
        assert PipelineTokens.STATUS == 'status'
        # Defensive: confirm the misleading name does NOT show up anywhere
        # on the constant class.
        for attr in dir(PipelineTokens):
            if attr.startswith('_'):
                continue
            value = getattr(PipelineTokens, attr)
            if isinstance(value, str):
                assert value != 'pipeline_status', (
                    f"PipelineTokens.{attr} = 'pipeline_status' is wrong! "
                    "Field on pipeline-tokens table is 'status' (without prefix)."
                )

    def test_backfill_specific_fields(self):
        """Per ADR #51. Renaming any of these requires updating both the
        SFN bulk-backfill template and the route handlers."""
        assert PipelineTokens.TARGET_SEED == 'target_seed'
        assert PipelineTokens.TARGET_PIPELINE == 'target_pipeline'
        assert PipelineTokens.TOTAL_PARTITIONS == 'total_partitions'
        assert PipelineTokens.COMPLETED_PARTITIONS == 'completed_partitions'
        assert PipelineTokens.FAILED_PARTITIONS == 'failed_partitions'
        assert PipelineTokens.SKIPPED_PARTITIONS == 'skipped_partitions'
        assert PipelineTokens.PARENT_BACKFILL_ID == 'parent_backfill_id'


class TestTaskStatusValues:
    """Allowed status values written by SFN templates, read by route handlers."""

    def test_canonical_values(self):
        assert TaskStatus.SUCCESS == 'success'
        assert TaskStatus.FAILED == 'failed'
        assert TaskStatus.SKIPPED == 'skipped'

    def test_successful_set_excludes_failed(self):
        """SUCCESSFUL means "counts as done for skip_completed semantics".
        FAILED is NOT successful — re-running a failed partition is exactly
        what backfill is for."""
        assert TaskStatus.FAILED not in TaskStatus.SUCCESSFUL
        assert TaskStatus.SUCCESS in TaskStatus.SUCCESSFUL
        assert TaskStatus.SKIPPED in TaskStatus.SUCCESSFUL

    def test_succeeded_present_alongside_success(self):
        """Regression test: this class's SUCCESSFUL frozenset previously only
        had 'success', missing the distinct 'succeeded' value that the
        canonical TASK_SUCCESS_STATUSES (constants_generated.py) includes.
        SUCCESSFUL is consumed directly by EE's backfill.py
        (make_output_missing_adapter's successful_statuses parameter, and
        _scan_completed_partitions' skip_completed check) — a task somehow
        ending up with status 'succeeded' would have been incorrectly
        treated as "output missing", forcing an unnecessary rebuild during
        backfill lineage-frontier computation."""
        assert TaskStatus.SUCCEEDED == 'succeeded'
        assert TaskStatus.SUCCEEDED in TaskStatus.SUCCESSFUL
