import type { WarningKind } from '@/generated/enums';

/** How the bell names each kind of coordination warning (ADR #130 §9). A Record over the
 *  generated union, so a kind added in polyris/constants.py fails the typecheck until named here. */
export const WARNING_KIND_LABELS: Record<WarningKind, string> = {
    coordination: 'Coordination problem',
    zombie: 'Task can no longer be woken',
    push: 'Asset-scheduled run not started',
    sweeper: 'Wake-up was delayed',
    notify: 'Dependent tasks not notified',
    manual: 'Manual action kept the real output',
    fact: 'Asset fact not recorded',
};

export function warningKindLabel(kind: string | undefined): string {
    return (kind && kind in WARNING_KIND_LABELS) ? WARNING_KIND_LABELS[kind as WarningKind] : 'Coordination warning';
}
