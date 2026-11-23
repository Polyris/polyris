import type { WarningKind } from '@/generated/enums';

/** How the bell names each kind of coordination warning (ADR #130 §9). A Record over the
 *  generated union, so a kind added in polyris/constants.py fails the typecheck until named here. */
export const WARNING_KIND_LABELS: Record<WarningKind, string> = {
    coordination: 'Coordination problem',
    push: 'Asset-scheduled run not started',
    sweeper: 'Wake-up was delayed',
    manual: 'Manual action kept the real output',
    step: 'Pipeline step failed',
};

export function warningKindLabel(kind: string | undefined): string {
    return (kind && kind in WARNING_KIND_LABELS) ? WARNING_KIND_LABELS[kind as WarningKind] : 'Coordination warning';
}
