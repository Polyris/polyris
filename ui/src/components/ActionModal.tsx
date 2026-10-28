import React from 'react';
import { Modal } from '@/components/Modal';
import { Check, SkipForward, AlertTriangle } from '@/utils/icons';
import { ErrorDisplay } from '@/components/TaskDetailModal/ErrorDisplay';
import type { TaskError } from '@/types';
import type { TaskDecisionContext } from '@/hooks/usePipelineActions';

interface ModalState {
    isOpen: boolean;
    title: string;
    message: string | null;
    icon: React.ReactNode;
    confirmText: string;
    confirmStyle?: string;
    action: string | null;
    customContent?: boolean;
    toRun?: string[] | null;
    toSkip?: string[] | null;
    taskContext?: TaskDecisionContext | null;
}

interface ActionModalProps {
    modal: ModalState;
    onClose: () => void;
    onConfirm: () => void;
    loading: boolean;
    pipelineName: string;
    triggerParams: string;
    onTriggerParamsChange: (value: string) => void;
}

function statusLabel(status: string): string {
    const map: Record<string, string> = {
        waiting: 'waiting',
        waiting_decision: 'decision',
        running: 'running',
        failed: 'failed',
        success: 'done',
        succeeded: 'done',
        skipped: 'skipped',
        stopped: 'stopped',
        aborted: 'aborted',
        upstream_failed: 'upstream failed',
    };
    return map[status] ?? status;
}

function hasVisibleError(error: TaskError | string | null): boolean {
    if (!error) return false;
    if (typeof error === 'string') return error.length > 0;
    return (error.Cause ?? '').length > 0 || (error.Error ?? '').length > 0;
}

function DecisionContext({ ctx }: { ctx: TaskDecisionContext }) {
    const hasAttempts = ctx.attempt > 1 || ctx.retryAttempt > 0;
    const withWaitFor = ctx.directDownstream.filter(d => d.hasWaitFor);

    if (!hasVisibleError(ctx.error) && !hasAttempts && ctx.directDownstream.length === 0 && withWaitFor.length === 0) {
        return null;
    }

    return (
        <div className="am-decision">
            {hasVisibleError(ctx.error) && (
                <div className="am-decision-section">
                    <div className="am-decision-label">Error</div>
                    <ErrorDisplay error={ctx.error} />
                </div>
            )}
            {hasAttempts && (
                <div className="am-decision-attempts">
                    {ctx.attempt > 1 && <span>Manual restart {ctx.attempt - 1}</span>}
                    {ctx.retryAttempt > 0 && (
                        <span>
                            Auto-retry {ctx.retryAttempt}
                            {ctx.maxRetries > 0 ? ` of ${ctx.maxRetries}` : ''}
                        </span>
                    )}
                </div>
            )}
            {ctx.directDownstream.length > 0 && (
                <div className="am-decision-section">
                    <div className="am-decision-label">Next tasks</div>
                    <ul className="am-decision-list">
                        {ctx.directDownstream.map(d => (
                            <li key={d.name} className="am-decision-task">
                                <span className="am-decision-task-name">{d.name}</span>
                                <span className="am-decision-task-status">{statusLabel(d.status)}</span>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
            {withWaitFor.length > 0 && (
                <div className="am-decision-warn">
                    <AlertTriangle size={13} className="am-decision-warn-icon" />
                    <span>
                        {withWaitFor.length === 1
                            ? `${withWaitFor[0].name} waits for data that may not have been produced`
                            : `${withWaitFor.length} downstream tasks wait for data that may not have been produced`}
                    </span>
                </div>
            )}
        </div>
    );
}

/**
 * ActionModal — Confirmation modal for pipeline actions (run, stop, pause, etc).
 * Shows run/skip breakdown and parameter input for run actions.
 */
export function ActionModal({
    modal,
    onClose,
    onConfirm,
    loading,
    pipelineName,
    triggerParams,
    onTriggerParamsChange,
}: ActionModalProps) {
    return (
        <Modal
            isOpen={modal.isOpen}
            onClose={onClose}
            onConfirm={onConfirm}
            title={modal.title}
            message={modal.message}
            icon={modal.icon}
            confirmText={modal.confirmText}
            loading={loading}
            confirmVariant={
                modal.confirmStyle === 'btn-danger' ? 'destructive' :
                modal.confirmStyle === 'btn-warning' ? 'destructive' :
                modal.confirmStyle === 'btn-secondary' ? 'secondary' :
                'default'
            }
        >
            {modal.taskContext && (modal.action === 'skip' || modal.action === 'success') && (
                <DecisionContext ctx={modal.taskContext} />
            )}
            {modal.customContent && modal.action === 'runPipeline' && (
                <div>
                    <p className="mb-md">
                        Start a new execution of <strong>{pipelineName}</strong>
                    </p>

                    {(modal.toRun || modal.toSkip) && (
                        <div className="mb-md">
                            <details className="am-run-details">
                                <summary className="am-run-summary">
                                    <span className="am-run-count am-run-count-success">
                                        <Check size={14} className="inline mr-xs" />
                                        {modal.toRun?.length || 0} to run
                                    </span>
                                    <span className="am-run-count am-run-count-skip">
                                        <SkipForward size={14} className="inline mr-xs" />
                                        {modal.toSkip?.length || 0} to skip
                                    </span>
                                </summary>
                                <div className="am-run-details-content">
                                    {(modal.toRun?.length ?? 0) > 0 && (
                                        <div className="mb-sm">
                                            <div className="text-xs text-success font-medium mb-xs">Will run:</div>
                                            <div className="am-run-task-list">{modal.toRun!.join(', ')}</div>
                                        </div>
                                    )}
                                    {(modal.toSkip?.length ?? 0) > 0 && (
                                        <div>
                                            <div className="text-xs text-muted font-medium mb-xs">Will skip:</div>
                                            <div className="am-run-task-list text-muted">{modal.toSkip!.join(', ')}</div>
                                        </div>
                                    )}
                                </div>
                            </details>
                        </div>
                    )}

                    <div className="mb-sm text-md font-medium">Input Parameters (JSON):</div>
                    <textarea
                        className="am-params-textarea"
                        aria-label="Input parameters JSON"
                        value={triggerParams}
                        onChange={(e) => onTriggerParamsChange(e.target.value)}
                        placeholder='{"current_date": "2024-01-01", "custom_param": "value"}'
                    />
                    <div className="mt-sm text-xs text-muted">
                        Add any custom parameters your pipeline accepts
                    </div>
                </div>
            )}
        </Modal>
    );
}
