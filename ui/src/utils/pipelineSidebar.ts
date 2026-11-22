import type { PipelineWithUI } from '@/types';
import type { PipelineStatus } from '@/generated/enums';
import { matchesSearch } from './search';

/**
 * The pipelines sidebar as a flat list of lines, built from the pipeline list.
 *
 * Kept pure so every rule has a table test (`pipelineSidebar.test.ts`):
 * - pipelines that need the viewer (latest run failed, has a failed task while it
 *   still runs, or waits for their decision) are pinned under "Needs attention", and
 *   stay in their group too;
 * - groups come only from `DAG(group=)`, trimmed and compared without case; a
 *   pipeline without one goes to "Ungrouped", listed last. With no group at
 *   all, the list is flat;
 * - a search matches a pipeline's name and group; the matches keep their
 *   groups (all open), with no "Needs attention".
 *
 * A pipeline's `status` is its latest run within RECENT_WINDOW_DAYS
 * (`utils/historyWindow.ts`), or 'idle' when it has none there.
 */

/**
 * Whether a latest-run status is a failure the viewer has to act on. A running
 * run has its spinner; a stop is not a failure (ADR #112). A Record, so a new
 * PipelineStatus does not compile until it is placed here.
 */
const FAILURE: Record<PipelineStatus, boolean> = {
    failed: true,
    timed_out: true,
    running: false,
    aborted: false,
    recovered: false,
    success: false,
    idle: false,
};

/** The latest-run statuses that count as a failure. */
export const FAILURE_STATUSES: ReadonlyArray<PipelineStatus> =
    (Object.keys(FAILURE) as PipelineStatus[]).filter(status => FAILURE[status]);

export function isFailure(status: string | undefined): boolean {
    return !!status && Object.hasOwn(FAILURE, status) && FAILURE[status as PipelineStatus];
}

/** True when a pipeline needs the viewer: its latest run failed, has a failed task while it still runs
 *  (it ends only once every task has, ADR #137 §4), or waits for their decision. */
export function needsAttention(p: Pick<PipelineWithUI, 'status' | 'waiting_on' | 'failed_tasks'>): boolean {
    return isFailure(p.status) || p.waiting_on === 'waiting_decision'
        || (p.status === 'running' && (p.failed_tasks ?? 0) > 0);
}

/** Group key of the pipelines that set no `group=`. Shown as "Ungrouped". */
export const UNGROUPED = '';

/** The key a pipeline is grouped by: its `group=`, trimmed, without case. */
export function groupKey(p: Pick<PipelineWithUI, 'group'>): string {
    return (p.group ?? '').trim().toLowerCase();
}

export type SidebarSection = 'attention' | 'all' | 'matches';

export type SidebarLine =
    | {
        kind: 'section'; key: string; section: SidebarSection; count: number;
        /** The viewer can fold it ("Needs attention" only). */
        foldable: boolean; open: boolean;
    }
    | {
        kind: 'group'; key: string;
        /** Normalized key (`groupKey`); what folding stores. */
        group: string;
        /** As the first pipeline (A–Z) spells it; "" for Ungrouped. */
        label: string;
        count: number; open: boolean;
        /** The open pipeline is in this group. */
        current: boolean;
        /** False while searching: every group of the matches is shown open. */
        foldable: boolean;
    }
    | { kind: 'row'; key: string; pipeline: PipelineWithUI; indented: boolean };

interface Group { key: string; label: string; items: PipelineWithUI[] }

/** Groups of `sorted` (A–Z), A–Z by key, Ungrouped last. */
function groupsOf(sorted: ReadonlyArray<PipelineWithUI>): Group[] {
    const byKey = new Map<string, Group>();
    sorted.forEach(p => {
        const key = groupKey(p);
        const group = byKey.get(key);
        if (group) group.items.push(p);
        else byKey.set(key, { key, label: (p.group ?? '').trim(), items: [p] });
    });
    const groups = [...byKey.values()].filter(g => g.key !== UNGROUPED).sort((a, b) => a.key.localeCompare(b.key));
    const ungrouped = byKey.get(UNGROUPED);
    return ungrouped ? [...groups, ungrouped] : groups;
}

export function buildSidebarLines(
    pipelines: ReadonlyArray<PipelineWithUI>,
    { search, folded, selected = null, attentionFolded = false }: {
        search: string;
        /** Group keys the viewer folded. A list, not a Record: a group may be named `constructor`. */
        folded: ReadonlyArray<string>;
        /** Name of the open pipeline, if any. */
        selected?: string | null;
        /** The viewer folded "Needs attention". */
        attentionFolded?: boolean;
    },
): SidebarLine[] {
    const sorted = [...pipelines].sort((a, b) => a.name.localeCompare(b.name));
    const lines: SidebarLine[] = [];
    const row = (p: PipelineWithUI, place: string, indented: boolean): SidebarLine =>
        ({ kind: 'row', key: `row:${place}:${p.name}`, pipeline: p, indented });
    const searching = !!search.trim();

    const listed = searching ? sorted.filter(p => matchesSearch(`${p.name} ${p.group ?? ''}`, search)) : sorted;
    if (listed.length === 0) return [];

    if (searching) {
        lines.push({ kind: 'section', key: 'section:matches', section: 'matches', count: listed.length, foldable: false, open: true });
    } else {
        const attention = sorted.filter(needsAttention);
        if (attention.length > 0) {
            lines.push({
                kind: 'section', key: 'section:attention', section: 'attention', count: attention.length,
                foldable: true, open: !attentionFolded,
            });
            if (!attentionFolded) attention.forEach(p => lines.push(row(p, 'attention', false)));
            lines.push({ kind: 'section', key: 'section:all', section: 'all', count: sorted.length, foldable: false, open: true });
        }
    }

    const groups = groupsOf(listed);
    if (groups.length === 1 && groups[0].key === UNGROUPED) {
        listed.forEach(p => lines.push(row(p, searching ? 'match' : 'all', false)));
        return lines;
    }

    groups.forEach(({ key, label, items }) => {
        const open = searching || !folded.includes(key);
        lines.push({
            kind: 'group',
            key: `group:${key}`,
            group: key,
            label,
            count: items.length,
            open,
            current: items.some(p => p.name === selected),
            foldable: !searching,
        });
        if (open) items.forEach(p => lines.push(row(p, `group:${key}`, true)));
    });
    return lines;
}

/** Why a pipeline is marked: the registry's format_version is missing or older than the platform reads
 *  (`needs_redeploy` from /api/pipelines, ADR #137). */
export const REDEPLOY_HINT = 'Redeploy needed: run polyris-deploy for this pipeline; its runs fail until then.';
