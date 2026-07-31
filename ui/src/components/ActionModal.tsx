import React from 'react';
import { Modal } from '@/components/Modal';
import { Check, SkipForward } from '@/utils/icons';

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
