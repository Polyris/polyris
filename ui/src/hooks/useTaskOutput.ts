import { useState, useEffect } from 'react';
import { api } from '../utils';

interface TaskOutputState {
    input: unknown;
    output: unknown;
    truncated: boolean;
    loading: boolean;
    loaded: boolean;
    /** `run_id` stamped on the canonical `output#*` row (from the wrapper's
     * Init_Output_Row / Save_Canonical_Output). Compared against
     * `expectedRunId` by OutputCard so cross-run stale rows are gated. */
    outputRowRunId: string | null;
    /** `run_id` stamped on the `input#*` row (from Save_Input_Record).
     * InputSection compares to detect stale-input-from-prior-run. */
    inputRowRunId: string | null;
    /** The run_task_helper ARN this run's wrapper invoked. Set by the
     * dependency_wrapper on the per-run task row. Compared to row-run-ids;
     * mismatch means the row content belongs to another same-date run. */
    expectedRunId: string | null;
}

/**
 * useTaskOutput — fetch a task's stored input and output when the Input/Output tab
 * is active. Mirrors useTaskEvents: fetch on open, guard against setting state after
 * unmount.
 */
export function useTaskOutput(
    task: { task_name?: string; execution_name?: string; date?: string; pipeline_execution?: string } | null | undefined,
    active: boolean,
): TaskOutputState {
    const [state, setState] = useState<TaskOutputState>({
        input: null, output: null, truncated: false, loading: false, loaded: false,
        outputRowRunId: null, inputRowRunId: null, expectedRunId: null,
    });

    const name = task?.execution_name || task?.task_name;
    const date = task?.date || '';
    const pipelineExecution = task?.pipeline_execution || '';

    useEffect(() => {
        if (!name || !active) {
            return;
        }

        let isMounted = true;

        const fetchOutput = async () => {
            setState(s => ({ ...s, loading: true }));
            try {
                const params = new URLSearchParams({ name });
                if (date) params.set('date', date);
                if (pipelineExecution) params.set('pipeline_execution', pipelineExecution);
                const resp = await api.get(`/task-output?${params.toString()}`);
                if (isMounted) {
                    setState({
                        input: resp ? resp.input : null,
                        output: resp ? resp.output : null,
                        truncated: !!(resp && resp.truncated),
                        loading: false,
                        loaded: true,
                        outputRowRunId: resp?.output_row_run_id ?? null,
                        inputRowRunId: resp?.input_row_run_id ?? null,
                        expectedRunId: resp?.expected_run_id ?? null,
                    });
                }
            } catch (e) {
                console.error('Failed to fetch task output:', e);
                if (isMounted) {
                    setState({
                        input: null, output: null, truncated: false, loading: false, loaded: true,
                        outputRowRunId: null, inputRowRunId: null, expectedRunId: null,
                    });
                }
            }
        };

        fetchOutput();

        return () => {
            isMounted = false;
        };
    }, [name, date, pipelineExecution, active]);

    return state;
}
