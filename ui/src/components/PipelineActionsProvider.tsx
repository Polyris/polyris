'use client';

import React from 'react';
import { usePipelineActions } from '@/hooks/usePipelineActions';
import { ActionModal } from '@/components/ActionModal';
import type { PipelineActionsParams, PipelineActions } from '@/types';

interface PipelineActionsProviderProps {
    params: PipelineActionsParams;
    children: (actions: PipelineActions) => React.ReactNode;
}

/**
 * Team-tier host for pipeline action handling (ADR #99). Owns the
 * usePipelineActions hook and the confirmation ActionModal, and exposes the
 * action handlers to the (free) PipelineDetail host via a render-prop.
 *
 * In the OSS build this provider is absent, so PipelineDetail renders its
 * content without action handlers and its intervention / task-action UI is
 * gated off.
 */
export function PipelineActionsProvider({ params, children }: PipelineActionsProviderProps) {
    const actions = usePipelineActions(params);
    return (
        <>
            {children(actions)}
            <ActionModal
                modal={actions.modal}
                onClose={actions.closeModal}
                onConfirm={actions.executeModalAction}
                loading={actions.actionPending}
                pipelineName={params.selectedPipeline?.name ?? ''}
                triggerParams={actions.triggerParams}
                onTriggerParamsChange={actions.setTriggerParams}
            />
        </>
    );
}

export default PipelineActionsProvider;
