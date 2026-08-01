import { logger } from '@/utils/logger';
import { TASK_SETTLED_STATUSES } from '@/generated/enums';
import { toDateString } from '@/utils/formatters';
import { useState, useCallback } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api, getUpstreamTasks, getDownstreamTasks, getApiErrorMessage } from '@/utils';
import { queryKeys } from '@/lib/queryClient';
import { 
    Rocket, 
    StopCircle, 
    SkipForward, 
    XCircle, 
    CheckCircle2,
    RotateCcw, 
    Target, 
    Play,
    CircleDot
} from '@/utils/icons';

import type { Task, Execution, SelectedExecution } from '@/types';
import type { PipelineActionsParams } from '@/types';

/**
 * Resolve "the execution these pipeline-level actions (pause/resume/extend/stop)
 * should target": the user's explicit selection if it still exists in the list,
 * otherwise whichever execution is currently running.
 *
 * Must NOT be a single `.find(ex => ex.id === selected || ex.status === 'running')`
 * — that has no priority between the two branches, so whichever condition an
 * element satisfies first in array order wins. If a different, more recent
 * execution is running and happens to appear earlier than the selected one
 * (typical for a newest-first list), that OR-form silently targets the wrong
 * execution — pausing/stopping/extending a run the user isn't even looking at.
 */
function resolveCurrentExecution(
    executions: Execution[],
    selectedExecution: SelectedExecution | null
): Execution | undefined {
    if (selectedExecution?.execution_id) {
        const selected = executions.find(ex => ex.execution_id === selectedExecution.execution_id);
        if (selected) return selected;
    }
    return executions.find(ex => ex.status === 'running');
}

/**
 * usePipelineActions - Hook for managing pipeline and task actions
 * Uses React Query invalidation instead of direct state setters
 */
export function usePipelineActions({
    selectedPipeline,
    selectedTask,
    selectedExecution,
    executions,
    tasks,
    dag,
    date,
    setDate,
    setSelectedExecution,
    showToast,
    onSelectTask
}: PipelineActionsParams) {
    const queryClient = useQueryClient();
    
    // Modal state
    const [modal, setModal] = useState<{ isOpen: boolean; action: string | null; title: string; message: string | null; icon: React.ReactNode; confirmText: string; confirmStyle: string; customContent: boolean; toRun: string[] | null; toSkip: string[] | null }>({ 
        isOpen: false, 
        action: null, 
        title: '', 
        message: '', 
        icon: '', 
        confirmText: '', 
        confirmStyle: '',
        customContent: false,
        toRun: null,
        toSkip: null
    });
    const [triggerParams, setTriggerParams] = useState('{}');
    const [executionPaused, setExecutionPaused] = useState(false);
    const [actionPending, setActionPending] = useState(false);
    // Which action is in flight, for button loading labels: 'run' | 'pause' | 'resume' | 'stop'.
    const [pendingAction, setPendingAction] = useState<string | null>(null);
    
    // ========== Invalidation Helpers ==========
    // eslint-disable-next-line react-hooks/preserve-manual-memoization -- manual useCallback kept intentionally
    const invalidateAll = useCallback(() => {
        queryClient.invalidateQueries({ queryKey: queryKeys.pipelines });
        if (selectedPipeline?.name) {
            queryClient.invalidateQueries({ queryKey: ['pipeline', selectedPipeline.name] });
        }
    }, [queryClient, selectedPipeline?.name]);
    
    // ========== Refresh Helper ==========
    const handleRefresh = useCallback(() => {
        invalidateAll();
    }, [invalidateAll]);
    
    // ========== Pipeline Actions ==========
    const handleRun = useCallback(() => {
        if (!selectedPipeline) return;
        // Always default to today, regardless of the date currently being
        // viewed in the pipeline detail page — Run means "run now"; running
        // for a specific past date is Backfill's job, not this button's.
        // The field below stays editable for anyone who wants to override it.
        setTriggerParams(JSON.stringify({ current_date: toDateString(new Date()) }, null, 2));
        setModal({
            isOpen: true,
            action: 'runPipeline',
            title: 'Run Pipeline',
            message: null,
            icon: <Rocket size={20} className="text-primary" />,
            confirmText: 'Run',
            confirmStyle: 'btn-primary',
            customContent: true,
            toRun: null,
            toSkip: null
        });
    }, [selectedPipeline]);
    
    const handleStop = useCallback(() => {
        if (!selectedPipeline) return;
        setModal({
            isOpen: true,
            action: 'stopPipeline',
            title: 'Stop Pipeline',
            message: `Stop all running tasks in "${selectedPipeline.name}"?`,
            icon: <StopCircle size={20} className="text-red-500" />,
            confirmText: 'Stop All',
            confirmStyle: 'btn-danger',
            customContent: false,
            toRun: null,
            toSkip: null
        });
    }, [selectedPipeline]);
    
    const handlePauseResume = useCallback(async () => {
        if (!selectedPipeline) return;
        
        const currentExecution = resolveCurrentExecution(executions, selectedExecution);
        
        if (!currentExecution?.execution_id) {
            showToast('No active execution to pause', 'warning');
            return;
        }
        
        const pipelineExecution = currentExecution.execution_id;
        
        setPendingAction(executionPaused ? 'resume' : 'pause');
        try {
            if (executionPaused) {
                const result = await api.post(`/execution-resume?id=${pipelineExecution}`, {});
                if (result.error) {
                    showToast(getApiErrorMessage(result), 'error');
                } else {
                    setExecutionPaused(false);
                    showToast('Pipeline resumed', 'success');
                    handleRefresh();
                }
            } else {
                const result = await api.post(`/execution-pause?id=${pipelineExecution}`, {});
                if (result.error) {
                    showToast(getApiErrorMessage(result), 'error');
                } else {
                    setExecutionPaused(true);
                    showToast('Pipeline paused – running tasks will complete, new tasks will wait', 'info');
                }
            }
        } catch (e: unknown) {
            showToast(`Failed to ${executionPaused ? 'resume' : 'pause'}: ${e instanceof Error ? e.message : String(e)}`, 'error');
        } finally {
            setPendingAction(null);
        }
    }, [selectedPipeline, executions, selectedExecution, executionPaused, showToast, handleRefresh]);
    
    const handleExtendPause = useCallback(async () => {
        const currentExecution = resolveCurrentExecution(executions, selectedExecution);
        
        if (!currentExecution?.execution_id) return;
        
        try {
            const result = await api.post(`/execution-extend?id=${currentExecution.execution_id}`, {});
            if (result.error) {
                showToast(getApiErrorMessage(result), 'error');
            } else {
                showToast('Pause extended by 12 hours', 'success');
            }
        } catch (e: unknown) {
            showToast(`Failed to extend pause: ${e instanceof Error ? e.message : String(e)}`, 'error');
        }
    }, [executions, selectedExecution, showToast]);
    
    // ========== Task Actions ==========
    const handleTaskAction = useCallback((action: string, task: Task | null = null) => {
        const targetTask = task || selectedTask;
        if (!targetTask) return;
        
        const configs: Record<string, { title: string; message: string; icon: React.ReactNode; confirmText: string; confirmStyle: string }> = {
            skip: { 
                title: 'Skip Task', 
                message: `Skip "${targetTask.task_name}"? This will mark it as complete and trigger dependent tasks.`, 
                icon: <SkipForward size={20} className="text-amber-500" />, 
                confirmText: 'Skip', 
                confirmStyle: 'btn-warning' 
            },
            stop: { 
                title: 'Stop Task', 
                message: `Stop "${targetTask.task_name}"? The execution will be terminated. You can restart it later.`, 
                icon: <StopCircle size={20} className="text-amber-500" />, 
                confirmText: 'Stop', 
                confirmStyle: 'btn-secondary' 
            },
            fail: { 
                title: 'Mark Failed', 
                message: `Mark "${targetTask.task_name}" as failed? This will fail the pipeline.`, 
                icon: <XCircle size={20} className="text-red-500" />, 
                confirmText: 'Mark Failed', 
                confirmStyle: 'btn-danger' 
            },
            success: { 
                title: 'Mark Successful', 
                message: `Mark "${targetTask.task_name}" as successful? Use when work completed outside the pipeline.`, 
                icon: <CheckCircle2 size={20} className="text-green-500" />, 
                confirmText: 'Mark Successful', 
                confirmStyle: 'btn-success' 
            },
            restart: { 
                title: 'Restart Task', 
                message: `Restart "${targetTask.task_name}"? This will stop the current execution and start fresh.`, 
                icon: <RotateCcw size={20} className="text-blue-500" />, 
                confirmText: 'Restart', 
                confirmStyle: 'btn-primary' 
            }
        };
        
        const config = configs[action];
        if (!config) return;
        
        onSelectTask(targetTask);
        setModal({ 
            isOpen: true, 
            action, 
            ...config, 
            customContent: false, 
            toRun: null, 
            toSkip: null 
        });
    }, [selectedTask, onSelectTask]);
    
    // ========== Partial Run Actions ==========
    const runToHere = useCallback((task: Task) => {
        const allTaskNames = dag?.nodes?.map(n => n.id) || [];
        const upstream = getUpstreamTasks(task.task_name, dag);
        const toRun = [...upstream, task.task_name];
        const toSkip = allTaskNames.filter(t => !toRun.includes(t));
        
        setTriggerParams(JSON.stringify({ current_date: date, skip_tasks: toSkip }, null, 2));
        setModal({
            isOpen: true,
            action: 'runPipeline',
            title: `Run to "${task.task_name}"`,
            message: `Will run ${toRun.length} task${toRun.length !== 1 ? 's' : ''}, skip ${toSkip.length}`,
            icon: <Target size={20} className="text-primary" />,
            confirmText: 'Run',
            confirmStyle: 'btn-primary',
            customContent: true,
            toRun,
            toSkip
        });
    }, [dag, date]);
    
    const runFromHere = useCallback((task: Task) => {
        const downstream = getDownstreamTasks(task.task_name, dag);
        const toRun = [task.task_name, ...downstream];
        const allTaskNames = dag?.nodes?.map(n => n.id) || [];
        const toSkip = allTaskNames.filter(t => !toRun.includes(t));
        
        setTriggerParams(JSON.stringify({ current_date: date, skip_tasks: toSkip }, null, 2));
        setModal({
            isOpen: true,
            action: 'runPipeline',
            title: `Run from "${task.task_name}"`,
            message: `Will run ${toRun.length} task${toRun.length !== 1 ? 's' : ''}, skip ${toSkip.length}`,
            icon: <Play size={20} className="text-primary" />,
            confirmText: 'Run',
            confirmStyle: 'btn-primary',
            customContent: true,
            toRun,
            toSkip
        });
    }, [dag, date]);
    
    const runOnlyThis = useCallback((task: Task) => {
        const allTaskNames = dag?.nodes?.map(n => n.id) || [];
        const toSkip = allTaskNames.filter(t => t !== task.task_name);
        
        setTriggerParams(JSON.stringify({ current_date: date, skip_tasks: toSkip }, null, 2));
        setModal({
            isOpen: true,
            action: 'runPipeline',
            title: `Run only "${task.task_name}"`,
            message: null,
            icon: <CircleDot size={20} className="text-primary" />,
            confirmText: 'Run',
            confirmStyle: 'btn-primary',
            customContent: true,
            toRun: [task.task_name],
            toSkip
        });
    }, [dag, date]);
    
    const handleRunAction = useCallback((actionType: string, task: Task) => {
        if (actionType === 'toHere') runToHere(task);
        else if (actionType === 'fromHere') runFromHere(task);
        else if (actionType === 'onlyThis') runOnlyThis(task);
    }, [runToHere, runFromHere, runOnlyThis]);
    
    // ========== Modal Execution ==========
    const executeModalAction = useCallback(async () => {
        const action = modal.action;
        
        if (action === 'close' || !action) {
            setModal(prev => ({ ...prev, isOpen: false }));
            return;
        }
        
        setActionPending(true);
        setPendingAction(action === 'runPipeline' ? 'run' : action === 'stopPipeline' ? 'stop' : null);
        
        if (!selectedPipeline) {
            setActionPending(false);
            setPendingAction(null);
            return;
        }
        
        try {
        if (action === 'runPipeline') {
            try {
                const params = JSON.parse(triggerParams);
                const result = await api.post(`/pipeline-run?name=${selectedPipeline.name}`, { input: params });
                setModal(prev => ({ ...prev, isOpen: false }));
                if (result.error) {
                    showToast(getApiErrorMessage(result), 'error');
                } else {
                    showToast('Pipeline started!', 'success');
                    const targetDate = params.current_date || date;
                    if (params.current_date) setDate(params.current_date);
                    
                    const execArn = result.execution_arn || null;
                    const execShort = execArn ? execArn.split(':').pop() : null;
                    
                    if (execArn) {
                        setSelectedExecution({
                            execution_id: execArn,
                            execution_short: execShort ?? undefined,
                            date: targetDate,
                            status: 'running',
                            start_time: new Date().toISOString(),
                            auto_selected: false
                        });
                    }
                    
                    // Aggressive refresh for first few seconds to catch countdown
                    const refreshTimes = [500, 1000, 1500, 2500, 4000, 6000, 8000];
                    refreshTimes.forEach(delay => setTimeout(() => {
                        invalidateAll();
                    }, delay));
                }
            } catch (e: unknown) {
                setModal(prev => ({ ...prev, isOpen: false }));
                showToast('Invalid JSON: ' + (e instanceof Error ? e.message : String(e)), 'error');
            }
            return;
        }
        
        if (action === 'stopPipeline') {
            const incompleteTasks = tasks.filter(t => 
                !TASK_SETTLED_STATUSES.includes(t.status)
            );
            
            for (const task of incompleteTasks) {
                if (task.execution_name) {
                    await api.post(`/task-stop?name=${task.execution_name}`, {});
                }
            }
            
            const currentExecution = resolveCurrentExecution(executions, selectedExecution);
            
            const execId = currentExecution?.execution_id || selectedExecution?.execution_id;
            if (execId) {
                try {
                    await api.post(`/execution-stop?pipeline_execution=${encodeURIComponent(execId)}`, {});
                } catch (e: unknown) {
                    logger.error('actions', 'Failed to stop orchestration', e);
                }
            }
            
            setModal(prev => ({ ...prev, isOpen: false }));
            invalidateAll();
            return;
        }
        
        // Task actions (skip, stop, fail, success, restart)
        if (!selectedTask) {
            setModal(prev => ({ ...prev, isOpen: false }));
            return;
        }
        const taskName = selectedTask.execution_name || `${selectedTask.task_name}-${date}`;
        const endpoints: Record<string, string> = { 
            skip: `/task-skip?name=${taskName}`, 
            stop: `/task-stop?name=${taskName}`, 
            fail: `/task-fail?name=${taskName}`, 
            success: `/task-success?name=${taskName}`,
            restart: `/task-restart?name=${taskName}` 
        };
        
        if (!endpoints[action]) {
            setModal(prev => ({ ...prev, isOpen: false }));
            return;
        }
        const result = await api.post(endpoints[action], { date });
        setModal(prev => ({ ...prev, isOpen: false }));
        if (result.error) {
            showToast(getApiErrorMessage(result), 'error');
        } else {
            const actionNames: Record<string, string> = { 
                skip: 'skipped', 
                stop: 'stopped', 
                fail: 'marked failed', 
                success: 'marked successful',
                restart: 'restarted' 
            };
            showToast(`Task ${actionNames[action] || action}`, 'success');
            invalidateAll();
        }
        } finally {
            setActionPending(false);
            setPendingAction(null);
        }
    }, [
        modal.action, triggerParams, selectedPipeline, selectedTask, tasks, executions,
        selectedExecution, date, setDate, setSelectedExecution,
        showToast, invalidateAll
    ]);
    
    const closeModal = useCallback(() => {
        setModal(prev => ({ ...prev, isOpen: false }));
    }, []);
    
    return {
        // Modal state
        modal,
        triggerParams,
        setTriggerParams,
        executionPaused,
        setExecutionPaused,
        actionPending,
        pendingAction,
        
        // Pipeline actions
        handleRun,
        handleStop,
        handlePauseResume,
        handleExtendPause,
        handleRefresh,
        
        // Task actions
        handleTaskAction,
        handleRunAction,
        
        // Modal actions
        executeModalAction,
        closeModal
    };
}
