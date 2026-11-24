import React, { useEffect, useMemo, useRef, useState } from 'react';
import { PipelineListSkeleton } from './Skeletons';
import { Button } from '@/components/ui/button';
import { EmptyState } from './EmptyState';
import {
    StatusIcon, Search, Inbox, ChevronDown, ChevronRight, Circle, X, AlertTriangle,
} from '../utils/icons';
import { formatRunStatus, formatSchedule } from '../utils/formatters';
import { connectionProblem } from '../utils/api';
import { logger } from '../utils/logger';
import { RECENT_WINDOW_DAYS } from '../utils/historyWindow';
import { buildSidebarLines, groupKey, isFailure, needsAttention, REDEPLOY_HINT, UNGROUPED } from '../utils/pipelineSidebar';
import type { SidebarLine, SidebarSection } from '../utils/pipelineSidebar';
import config from '../lib/config';
import { useAppStore } from '../stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { usePipelinesQuery, usePipelinesStatsNote } from '@/hooks/queries';
import { useIsMobileWidth, useKeyboardShortcuts, usePersistedState, transforms, SHORTCUTS } from '@/hooks';
import type { PipelineRowProps, PipelineWithUI, RunSparklineProps } from '@/types';

/** localStorage key of the groups the viewer folded: a JSON list of group keys. */
const FOLDED_GROUPS_KEY = 'sidebarFoldedGroups';
/** localStorage key: the viewer folded "Needs attention". */
const ATTENTION_FOLDED_KEY = 'sidebarAttentionFolded';
/** The sparkline always has this many slots, so its newest bar lines up across rows. */
const SPARK_SLOTS = 10;
const SECTION_LABELS: Record<SidebarSection, string> = {
    attention: 'Needs attention',
    all: 'All pipelines',
    matches: 'Matches',
};
/** How a run that waits for an operator reads in a row's accessible name. */
const WAITING_ON_LABELS = { waiting_decision: 'waiting for a decision', waiting_paused: 'paused' } as const;

const pipelineCount = (n: number) => `${n} pipeline${n === 1 ? '' : 's'}`;

function parseFoldedGroups(stored: string): string[] {
    try {
        const parsed: unknown = JSON.parse(stored);
        if (Array.isArray(parsed)) return parsed.filter((g): g is string => typeof g === 'string');
    } catch (e) {
        // Not JSON, so not written by this code: every group starts open.
        logger.warn('sidebar', 'Ignoring unreadable folded-groups value', e);
    }
    return [];
}

/** `/` must not pull focus out of an open dialog (a modal, ⌘K, a popover). */
const dialogOpen = () => !!document.querySelector('[aria-modal="true"], [role="dialog"]');

/** The pipeline a row key (`row:<place>:<name>`) is for; null for a section or group key. */
function rowPipelineName(key: string): string | null {
    if (!key.startsWith('row:')) return null;
    return key.split(':').slice(2).join(':');
}

/** "7 success, 2 failed" for the runs a sparkline draws (screen readers and the hover title). */
function describeRuns(runs: ReadonlyArray<{ status: string }>): string {
    if (runs.length === 0) return `no runs in the last ${RECENT_WINDOW_DAYS} days`;
    const counts = new Map<string, number>();
    runs.forEach(r => counts.set(r.status, (counts.get(r.status) ?? 0) + 1));
    return `last ${runs.length} run${runs.length === 1 ? '' : 's'}: ${[...counts].map(([s, n]) => `${n} ${s}`).join(', ')}`;
}

/**
 * PipelinesSidebar - the pipeline list on the Pipelines page.
 *
 * Lines come from `buildSidebarLines` (rules and tests live there). Keyboard:
 * `/` focuses the search, Enter there opens the first match and ↓ moves into
 * the list; in the list `j`/`k` or the arrows move through rows, groups and
 * the "Needs attention" header, Enter opens a pipeline or folds what it is on,
 * Esc goes back to the search. The list is one Tab stop (roving tabindex).
 *
 * While the pointer or the keyboard is in the list, a refetch does not move or
 * remove rows: one would otherwise shift under the click, or vanish with the
 * focus (a pipeline leaving "Needs attention"). Statuses, sparklines and a
 * folded group's count still update in place; the rows follow once both have
 * left, or once a pipeline is opened from the keyboard. A failed refetch keeps the last
 * list and says, above it, from when its statuses are (the System status says why); a
 * partial one (`stats_incomplete`, or `index_building` while the run index is built) is said there too.
 */
export function PipelinesSidebar() {
    const {
        selectedPipeline, setSelectedPipeline,
        pipelineSearch: search, setPipelineSearch,
        sidebarOpen: isOpen, setSidebarOpen,
    } = useAppStore(useShallow(s => ({
        selectedPipeline: s.selectedPipeline, setSelectedPipeline: s.setSelectedPipeline,
        pipelineSearch: s.pipelineSearch, setPipelineSearch: s.setPipelineSearch,
        sidebarOpen: s.sidebarOpen, setSidebarOpen: s.setSidebarOpen,
    })));
    const { data: fetched = [], isLoading: loading, isError, error, refetch, dataUpdatedAt } = usePipelinesQuery();
    const statsNote = usePipelinesStatsNote();
    const [held, setHeld] = useState<PipelineWithUI[] | null>(null);
    const holders = useRef({ pointer: false, focus: false });
    const latest = useMemo(() => new Map(fetched.map(p => [p.name, p])), [fetched]);
    // Rows (which, and where) come from the held list; what a row shows is always the fetched
    // pipeline. A pipeline that is gone leaves at once: its row would open nothing.
    const pipelines = useMemo(
        () => (held ? held.filter(p => latest.has(p.name)) : fetched),
        [held, fetched, latest],
    );
    // A folded group's count is a status, so it reads the fetched data too.
    const attentionByGroup = useMemo(() => {
        const counts = new Map<string, { failed: number; total: number }>();
        fetched.forEach(p => {
            if (!needsAttention(p)) return;
            const key = groupKey(p);
            const c = counts.get(key) ?? { failed: 0, total: 0 };
            c.total += 1;
            if (isFailure(p.status)) c.failed += 1;
            counts.set(key, c);
        });
        return counts;
    }, [fetched]);
    // Focus is in the list. Not cleared when the focused row unmounts (no blur fires), which is how
    // the effect below knows to move the focus to the same pipeline's other row.
    const focusWithin = useRef(false);
    // The effect below is moving the focus: the list's onFocus must not hold the rows again
    // after opening a pipeline let them go.
    const movingFocus = useRef(false);
    const hold = (who: 'pointer' | 'focus', on: boolean) => {
        holders.current[who] = on;
        const holding = holders.current.pointer || holders.current.focus;
        // Nothing to hold before the first load: an empty snapshot would hide the list once it arrives.
        setHeld(prev => (holding ? (prev ?? (fetched.length > 0 ? fetched : null)) : null));
    };
    const [folded, setFolded] = usePersistedState<string[]>(FOLDED_GROUPS_KEY, [], parseFoldedGroups, JSON.stringify);
    const [attentionFolded, setAttentionFolded] = usePersistedState<boolean>(ATTENTION_FOLDED_KEY, false, transforms.boolean);
    const [focusKey, setFocusKey] = useState<string | null>(null);
    const searchRef = useRef<HTMLInputElement>(null);
    const itemRefs = useRef(new Map<string, HTMLButtonElement>());

    const selectedName = selectedPipeline?.name ?? null;
    const lines = useMemo(
        () => buildSidebarLines(pipelines, { search, folded, selected: selectedName, attentionFolded }),
        [pipelines, search, folded, selectedName, attentionFolded],
    );
    // A pipeline can be listed twice (pinned and in its group); only its first shown row is marked and announced as current.
    const currentKey = lines.find(l => l.kind === 'row' && l.pipeline.name === selectedName)?.key ?? null;
    const searching = !!search.trim();
    // While searching, the count is the matches (the "Matches" section; none when nothing matches).
    const shownCount = searching ? lines.find(l => l.kind === 'section')?.count ?? 0 : pipelines.length;
    const groupKeys = useMemo(() => [...new Set(pipelines.map(groupKey))], [pipelines]);
    const hasGroups = groupKeys.some(k => k !== UNGROUPED);
    const allFolded = hasGroups && groupKeys.every(k => folded.includes(k));
    // The list's keyboard stops: rows, and every header that folds.
    const navigable = useMemo(
        () => lines.filter(l => l.kind === 'row' || l.foldable),
        [lines],
    );
    // The one item of the list in the Tab order: the last focused, else the open pipeline, else the first.
    const tabKey = useMemo(() => {
        if (focusKey && navigable.some(l => l.key === focusKey)) return focusKey;
        const open = navigable.find(l => l.kind === 'row' && l.pipeline.name === selectedName);
        return (open ?? navigable[0])?.key ?? null;
    }, [navigable, focusKey, selectedName]);

    const showSearch = pipelines.length > 3;
    // On a phone the sidebar is a drawer: closed, it is out of the Tab order and out of reach of `/`.
    const isPhone = useIsMobileWidth();
    const drawerClosed = isPhone && !isOpen;
    useKeyboardShortcuts(
        { [SHORTCUTS.FOCUS_FILTER]: () => { if (!drawerClosed && !dialogOpen()) searchRef.current?.focus(); } },
        { enabled: showSearch },
    );

    const focusItem = (key: string) => {
        setFocusKey(key);
        itemRefs.current.get(key)?.focus();
    };

    // The focused row left the list (its pipeline left "Needs attention", or is gone): keep the
    // focus on the same pipeline's row in its group, else on the item now where the row was,
    // rather than dropping it to <body> where j / k do nothing.
    const lastNavigable = useRef<SidebarLine[]>([]);
    useEffect(() => {
        const before = lastNavigable.current;
        lastNavigable.current = navigable;
        if (!focusWithin.current || !focusKey || navigable.some(l => l.key === focusKey)) return;
        const name = rowPipelineName(focusKey);
        const twin = name === null ? undefined : navigable.find(l => l.kind === 'row' && l.pipeline.name === name);
        const at = Math.max(0, before.findIndex(l => l.key === focusKey));
        const next = twin ?? navigable[Math.min(at, navigable.length - 1)];
        const el = next && itemRefs.current.get(next.key);
        if (el) {
            // The item's own onFocus records it as the list's Tab stop; the hold stays as it was.
            movingFocus.current = true;
            el.focus();
            movingFocus.current = false;
        } else {
            // Nothing left to focus: the focus is on <body>, and no blur said so. Let the rows go,
            // or a list held with nothing in it would stay empty while pipelines arrive.
            focusWithin.current = false;
            holders.current.focus = false;
            if (!holders.current.pointer) setHeld(null);
        }
    }, [navigable, focusKey]);
    const itemRef = (key: string) => (el: HTMLButtonElement | null) => {
        if (el) itemRefs.current.set(key, el);
        else itemRefs.current.delete(key);
    };

    const openPipeline = (p: PipelineWithUI) => {
        setSelectedPipeline(p);
        setSidebarOpen(false);
        // Opening a pipeline ends this use of the list: the focus stays on the row while the
        // viewer reads the DAG, and must not keep the rows still for that long.
        hold('focus', false);
    };

    const onListKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (e.key === 'Escape' && showSearch) {
            e.preventDefault();
            searchRef.current?.focus();
            return;
        }
        if (navigable.length === 0) return;
        const step = e.key === SHORTCUTS.NEXT_TASK || e.key === 'ArrowDown' ? 1
            : e.key === SHORTCUTS.PREV_TASK || e.key === 'ArrowUp' ? -1 : 0;
        if (!step) return;
        e.preventDefault();
        const at = navigable.findIndex(l => l.key === tabKey);
        focusItem(navigable[Math.max(0, Math.min(navigable.length - 1, at + step))].key);
    };

    const onSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'Escape') {
            if (search) {
                e.preventDefault();
                setPipelineSearch('');
            } else {
                searchRef.current?.blur();
            }
        } else if (e.key === 'Enter' && searching) {
            const first = lines.find(l => l.kind === 'row');
            if (first?.kind === 'row') {
                e.preventDefault();
                openPipeline(latest.get(first.pipeline.name) ?? first.pipeline);
            }
        } else if (e.key === 'ArrowDown' && navigable.length > 0) {
            e.preventDefault();
            focusItem(navigable[0].key);
        }
    };

    const toggleGroup = (group: string) => {
        setFolded(prev => (prev.includes(group) ? prev.filter(g => g !== group) : [...prev, group]));
    };

    const renderLine = (line: SidebarLine) => {
        if (line.kind === 'section') {
            if (!line.foldable) {
                return (
                    <h3 key={line.key} className={`sb-section sb-section--${line.section}`}>
                        <span>{SECTION_LABELS[line.section]}</span>
                        <span className="sb-section__count">{line.count}</span>
                    </h3>
                );
            }
            return (
                <h3 key={line.key} className={`sb-section sb-section--${line.section}`}>
                    <button
                        ref={itemRef(line.key)}
                        type="button"
                        className="sb-section__toggle"
                        tabIndex={line.key === tabKey ? 0 : -1}
                        aria-expanded={line.open}
                        onClick={() => setAttentionFolded(!attentionFolded)}
                        onFocus={() => setFocusKey(line.key)}
                    >
                        {line.open ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
                        <span className="sb-section__label">{SECTION_LABELS[line.section]}</span>
                        <span className="sb-section__count">{line.count}</span>
                    </button>
                </h3>
            );
        }
        if (line.kind === 'group') {
            const label = line.group === UNGROUPED ? 'Ungrouped' : line.label;
            if (!line.foldable) {
                // A group of search matches: a label, always open.
                return (
                    <div key={line.key} className="sb-group sb-group--static">
                        <span className="sb-group__label">{label}</span>
                        <span className="sb-group__count">{line.count}</span>
                    </div>
                );
            }
            // A folded group still says when something in it needs the viewer.
            const attention = attentionByGroup.get(line.group);
            const badge = line.open || !attention ? null
                : attention.failed === attention.total ? `${attention.total} failed`
                : `${attention.total} need${attention.total === 1 ? 's' : ''} attention`;
            // A folded group holding the open pipeline is marked, so the selection is never lost from view.
            const holdsOpen = !line.open && line.current;
            return (
                <button
                    key={line.key}
                    ref={itemRef(line.key)}
                    type="button"
                    className={`sb-group${holdsOpen ? ' sb-group--current' : ''}`}
                    tabIndex={line.key === tabKey ? 0 : -1}
                    aria-expanded={line.open}
                    aria-label={`${label} group, ${pipelineCount(line.count)}${badge ? `, ${badge}` : ''}${holdsOpen ? ', contains the open pipeline' : ''}`}
                    onClick={() => toggleGroup(line.group)}
                    onFocus={() => setFocusKey(line.key)}
                >
                    {line.open ? <ChevronDown size={12} aria-hidden="true" /> : <ChevronRight size={12} aria-hidden="true" />}
                    <span className="sb-group__label">{label}</span>
                    {badge && <span className="sb-badge">{badge}</span>}
                    <span className="sb-group__count">{line.count}</span>
                </button>
            );
        }
        const p = latest.get(line.pipeline.name) ?? line.pipeline;
        return (
            <PipelineRow
                key={line.key}
                pipeline={p}
                current={line.key === currentKey}
                indented={line.indented}
                tabIndex={line.key === tabKey ? 0 : -1}
                buttonRef={itemRef(line.key)}
                onFocus={() => setFocusKey(line.key)}
                onSelect={() => openPipeline(p)}
            />
        );
    };

    let body: React.ReactNode;
    if (loading) {
        body = <PipelineListSkeleton count={8} />;
    } else if (isError && pipelines.length === 0) {
        // A failed load is not an empty system: say so, and offer the retry (which cannot help
        // while the console has no API URL).
        body = (
            <EmptyState
                icon={AlertTriangle}
                tone="warning"
                role="alert"
                title="Couldn't load pipelines"
                description={config.API_URL
                    ? connectionProblem(error).message
                    : 'Configure API_URL in config.js to connect to your backend.'}
                action={config.API_URL
                    ? <Button size="sm" variant="secondary" onClick={() => { void refetch(); }}>Retry</Button>
                    : undefined}
            />
        );
    } else if (pipelines.length === 0) {
        body = (
            <EmptyState
                icon={Inbox}
                title="No pipelines"
                description={config.API_URL
                    ? 'No pipelines registered yet. Deploy a pipeline to see it here.'
                    : 'Configure API_URL in config.js to connect to your backend.'}
            />
        );
    } else if (lines.length === 0) {
        body = <EmptyState icon={Search} title="No matches" description={`No pipeline name or group matches "${search.trim()}"`} />;
    } else {
        body = lines.map(renderLine);
    }

    return (
        <>
        {/* Mobile overlay */}
        {isOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} aria-hidden="true" />}
        <nav className={`sidebar ${isOpen ? 'sidebar-open' : ''}`} aria-label="Pipelines" inert={drawerClosed}>
            <div className="sidebar-header">
                <span className="sidebar-title">Pipelines</span>
                <span className="sidebar-header__actions">
                    {hasGroups && !searching && (
                        <button
                            type="button"
                            className="sb-foldall"
                            onClick={() => setFolded(allFolded ? [] : groupKeys)}
                            aria-label={allFolded ? 'Unfold all groups' : 'Fold all groups'}
                        >
                            {/* Words, not an icon: at this size the chevrons read as a close button. */}
                            {allFolded ? 'Unfold all' : 'Fold all'}
                        </button>
                    )}
                    <span className="pipeline-count">
                        <span aria-hidden="true">{shownCount}</span>
                        <span className="sr-only">{pipelineCount(shownCount)}</span>
                    </span>
                </span>
            </div>

            {showSearch && (
                <div className="sidebar-search">
                    <div className="sb-search">
                        <Search size={14} className="sb-search__icon" aria-hidden="true" />
                        <input
                            ref={searchRef}
                            type="text"
                            className="sb-search__input"
                            placeholder="Search pipelines or groups"
                            aria-label="Search pipelines or groups"
                            value={search}
                            onChange={e => { setPipelineSearch(e.target.value); setFocusKey(null); }}
                            onKeyDown={onSearchKeyDown}
                        />
                        {search ? (
                            <button
                                type="button"
                                className="sb-search__clear"
                                aria-label="Clear search"
                                onClick={() => { setPipelineSearch(''); searchRef.current?.focus(); }}
                            >
                                <X size={14} aria-hidden="true" />
                            </button>
                        ) : (
                            <kbd className="sb-kbd" aria-hidden="true">/</kbd>
                        )}
                    </div>
                </div>
            )}

            {!loading && pipelines.length > 0 && (isError || statsNote) && (
                <div className="sb-note" role="status">
                    <AlertTriangle size={14} className="sb-note__icon" aria-hidden="true" />
                    <span>
                        {isError
                            ? `Couldn't refresh the list. Statuses as of ${new Date(dataUpdatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}.`
                            : statsNote === 'index_building'
                                ? 'The index of runs is still being built after the upgrade; statuses show once it is ready.'
                                : 'Some runs could not be read, so a status may be out of date.'}
                    </span>
                </div>
            )}

            <div
                className="sb-list"
                onKeyDown={onListKeyDown}
                onMouseEnter={() => hold('pointer', true)}
                onMouseLeave={() => hold('pointer', false)}
                onFocus={() => {
                    focusWithin.current = true;
                    if (!movingFocus.current) hold('focus', true);
                }}
                onBlur={e => {
                    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
                    focusWithin.current = false;
                    hold('focus', false);
                }}
            >
                {body}
            </div>
        </nav>
        </>
    );
}

/**
 * PipelineRow - one pipeline: latest-run status, name, a redeploy mark, last runs, schedule.
 */
function PipelineRow({ pipeline: p, current, indented, tabIndex, buttonRef, onFocus, onSelect }: PipelineRowProps) {
    // `status` is the latest run within the window /api/pipelines reads; 'idle' means none there.
    const status = p.status || 'idle';
    const waitingOn = status === 'running' && p.waiting_on ? p.waiting_on : null;
    const shown = formatRunStatus(status, p.failed_tasks);
    const lastRun = status === 'idle' ? `no run in the last ${RECENT_WINDOW_DAYS} days`
        : waitingOn ? `${shown}, ${WAITING_ON_LABELS[waitingOn]}`
        : shown;
    const schedule = formatSchedule(p.schedule, p.asset_schedule);
    const fullSchedule = formatSchedule(p.schedule, p.asset_schedule, { full: true });
    const runs = (p.recent_runs ?? []).slice(0, SPARK_SLOTS);
    const redeploy = p.needs_redeploy ? ', redeploy needed' : '';
    return (
        <button
            ref={buttonRef}
            type="button"
            className={`sb-row${indented ? ' sb-row--indented' : ''}${current ? ' sb-row--selected' : ''}`}
            aria-current={current ? 'page' : undefined}
            aria-label={`${p.name}${redeploy}, last run: ${lastRun}, ${describeRuns(runs)}, schedule: ${fullSchedule}`}
            title={`${p.name}${p.needs_redeploy ? `\n${REDEPLOY_HINT}` : ''}\nSchedule: ${fullSchedule}`}
            tabIndex={tabIndex}
            onFocus={onFocus}
            onClick={onSelect}
        >
            <span className="sb-row__icon" aria-hidden="true" title={`Last run: ${lastRun}`}>
                {status === 'idle'
                    ? <Circle size={9} strokeWidth={2.5} className="sb-row__idle" />
                    : <StatusIcon status={waitingOn ?? status} size={14} />}
            </span>
            <span className="sb-row__name">{p.name}</span>
            {p.needs_redeploy && <span className="sb-row__redeploy" aria-hidden="true">redeploy</span>}
            <RunSparkline runs={runs} />
            <span className="sb-row__schedule">{schedule}</span>
        </button>
    );
}

/**
 * RunSparkline - the last runs within RECENT_WINDOW_DAYS as bars, oldest on the
 * left. Empty slots pad the left side as a faint track, so the newest run is always
 * the rightmost bar. Colour tells the status; the row's accessible name and the
 * hover title spell the runs out.
 */
function RunSparkline({ runs }: RunSparklineProps) {
    // The API sends runs newest first.
    const ordered = runs.slice(0, SPARK_SLOTS).reverse();
    return (
        <span className="sb-spark" aria-hidden="true" title={describeRuns(runs)}>
            {Array.from({ length: SPARK_SLOTS - ordered.length }, (_, i) => (
                <span key={`empty-${i}`} className="sb-spark__bar" />
            ))}
            {ordered.map((run, i) => (
                <span
                    key={i}
                    className={`sb-spark__bar sb-spark__bar--${run.status}`}
                    title={`${run.date ?? ''}: ${formatRunStatus(run.status, run.failed_tasks)}`}
                />
            ))}
        </span>
    );
}

export default PipelinesSidebar;
