import { useState, useEffect } from 'react';
import { api } from '../utils';

interface TaskOutputState {
    input: unknown;
    output: unknown;
    truncated: boolean;
    loading: boolean;
    loaded: boolean;
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
                    });
                }
            } catch (e) {
                console.error('Failed to fetch task output:', e);
                if (isMounted) {
                    setState({ input: null, output: null, truncated: false, loading: false, loaded: true });
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
