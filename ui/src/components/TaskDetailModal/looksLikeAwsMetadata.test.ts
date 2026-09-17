import { describe, it, expect } from 'vitest';
import { looksLikeAwsMetadata } from './TaskDetailModal';

// Fixture responses captured from each AWS service integration the polyris
// wrapper runs `.sync`-style. Each is the shape a service task's wrapper
// writes as `task_output` when the user's job code does NOT call
// xcom.push(). The detector must fire on all of them — a missing key set
// means the Console never warns the user about a metadata-leak footgun for
// that task type.
//
// Coupled with `sam/sfn_templates/helpers/run_task/sfn.tpl.json`'s
// Run_Task_* branches (each writes `$states.result` verbatim). When a new
// service integration lands, add a fixture here + the wrapper key to
// looksLikeAwsMetadata's FLAT/WRAPPED sets. The Python-side parity test
// (`tests/sdk/test_xcom_coupled_constants_parity.py`) checks env-var and
// enum-value coupling; the response-shape coupling is UI-side only
// because the detector itself lives in TS with no Python counterpart.
describe('looksLikeAwsMetadata — per-service wrapper response shapes', () => {

    // ── Glue: startJobRun.sync ────────────────────────────────────────────
    it('detects Glue startJobRun.sync response', () => {
        expect(looksLikeAwsMetadata({
            JobRunId: 'jr_abc123',
            JobName: 'my-etl',
            JobRunState: 'SUCCEEDED',
            StartedOn: '2026-09-17T10:00:00Z',
            CompletedOn: '2026-09-17T10:05:00Z',
        })).toBe(true);
    });

    // ── Batch: submitJob.sync ─────────────────────────────────────────────
    it('detects Batch submitJob.sync response', () => {
        expect(looksLikeAwsMetadata({
            JobId: 'a1b2c3d4-...',
            JobName: 'nightly-report',
            JobArn: 'arn:aws:batch:us-east-1:...',
        })).toBe(true);
    });

    // ── Athena: startQueryExecution.sync ──────────────────────────────────
    it('detects Athena startQueryExecution.sync response (wrapper-outer shape)', () => {
        // Athena wraps everything under QueryExecution — pre-fix, the
        // detector missed this because it looked for top-level QueryExecutionId.
        expect(looksLikeAwsMetadata({
            QueryExecution: {
                QueryExecutionId: 'q-abc',
                EngineVersion: { EffectiveEngineVersion: 'Athena engine version 3' },
                Statistics: { DataScannedInBytes: 1024 },
                Status: { State: 'SUCCEEDED' },
            },
        })).toBe(true);
    });

    // ── ECS: runTask.sync ─────────────────────────────────────────────────
    it('detects ECS runTask.sync response (wrapper-outer shape with Tasks + Failures)', () => {
        expect(looksLikeAwsMetadata({
            Tasks: [{ TaskArn: 'arn:aws:ecs:...', LastStatus: 'STOPPED' }],
            Failures: [],
        })).toBe(true);
    });

    // ── EMR: addStep-ish (Step wrapper) ───────────────────────────────────
    it('detects EMR addStep-ish response (wrapper-outer Step shape)', () => {
        expect(looksLikeAwsMetadata({
            Step: {
                Id: 's-abc',
                Name: 'my-step',
                Status: { State: 'COMPLETED' },
            },
        })).toBe(true);
    });

    // ── child SFN: startExecution.sync ────────────────────────────────────
    it('detects child SFN startExecution.sync response', () => {
        expect(looksLikeAwsMetadata({
            ExecutionArn: 'arn:aws:states:...:execution:child:abc',
            StartDate: 1_700_000_000,
            StopDate: 1_700_000_600,
            Status: 'SUCCEEDED',
            Output: '{"real": "data"}',
        })).toBe(true);
    });

    // ── Real user output must NOT be detected as metadata ────────────────

    it('does NOT detect a small user dict as metadata', () => {
        expect(looksLikeAwsMetadata({ rows: 1240, path: 's3://…' })).toBe(false);
    });

    it('does NOT detect a nested user report as metadata', () => {
        expect(looksLikeAwsMetadata({
            summary: { total: 500, categories: 12 },
            timestamp: '2026-09-17',
        })).toBe(false);
    });

    it('does NOT detect primitives / arrays / null', () => {
        expect(looksLikeAwsMetadata(42)).toBe(false);
        expect(looksLikeAwsMetadata('a string')).toBe(false);
        expect(looksLikeAwsMetadata([1, 2, 3])).toBe(false);
        expect(looksLikeAwsMetadata(null)).toBe(false);
        expect(looksLikeAwsMetadata(undefined)).toBe(false);
        expect(looksLikeAwsMetadata({})).toBe(false);
    });

    it('does NOT detect a large user dict (> 6 top-level keys) even if it happens to include a metadata key name', () => {
        // Belt-and-suspenders — real service responses have small key counts;
        // a large object is almost certainly user data. Cap at 6 keys.
        expect(looksLikeAwsMetadata({
            JobRunId: 'this looks like metadata but is user data',
            a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7,
        })).toBe(false);
    });
});
