import React, { useMemo, useState } from 'react';
import { PipelineListSkeleton } from './Skeletons';
import { StatusIcon, Workflow, Search, Inbox, ChevronDown, ChevronRight } from '../utils/icons';
import { formatSchedule } from '../utils/formatters';
import config from '../lib/config';
import { useAppStore } from '../stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { usePipelinesQuery } from '@/hooks/queries';
import type { PipelineItemProps, RunSparklineProps, PipelineWithUI } from '@/types';

/**
 * PipelinesSidebar - Left sidebar showing list of pipelines.
 * Reads selection, search, and sidebar state from Zustand store.
 * Fetches pipelines directly via React Query.
 */
export function PipelinesSidebar() {
    const {
        selectedPipeline, setSelectedPipeline,
        pipelineSearch: search, setPipelineSearch: onSearchChange,
        sidebarOpen: isOpen, setSidebarOpen,
    } = useAppStore(useShallow(s => ({
        selectedPipeline: s.selectedPipeline, setSelectedPipeline: s.setSelectedPipeline,
        pipelineSearch: s.pipelineSearch, setPipelineSearch: s.setPipelineSearch,
        sidebarOpen: s.sidebarOpen, setSidebarOpen: s.setSidebarOpen,
    })));
    const { data: pipelines = [], isLoading: loading } = usePipelinesQuery();
    // Track which groups user has manually toggled
    const [manualToggles, setManualToggles] = useState<Record<string, boolean>>({});

    // Filter and sort pipelines by search (multi-word AND logic)
    const filteredPipelines = useMemo(() => {
        let result = [...pipelines];
        if (search.trim()) {
            const searchTerms = search.toLowerCase().trim().split(/\s+/);
            result = result.filter(p => {
                const name = p.name.toLowerCase();
                return searchTerms.every(term => name.includes(term));
            });
        }
        // Always sort alphabetically
        result.sort((a, b) => a.name.localeCompare(b.name));
        return result;
    }, [pipelines, search]);

    // Group pipelines by explicit group (with prefix fallback)
    const { groups, hasGroups } = useMemo(() => {
        const groupMap: Record<string, PipelineWithUI[]> = {};
        filteredPipelines.forEach(p => {
            // Use explicit group, fallback to prefix (text before first '-')
            let group = p.group;
            if (!group) {
                const dashIdx = p.name.indexOf('-');
                group = dashIdx > 0 ? p.name.substring(0, dashIdx) : '_ungrouped';
            }
            if (!groupMap[group]) groupMap[group] = [];
            groupMap[group].push(p);
        });
        return {
            groups: groupMap,
            hasGroups: Object.keys(groupMap).some(k => k !== '_ungrouped')
        };
    }, [filteredPipelines]);

    // Compute which groups should be expanded (auto logic + manual overrides)
    const expandedGroups = useMemo(() => {
        const auto: Record<string, boolean> = {};
        const totalPipelines = filteredPipelines.length;
        
        for (const [group, items] of Object.entries(groups)) {
            // If user manually toggled, respect that
            if (group in manualToggles) {
                auto[group] = manualToggles[group];
                continue;
            }
            // Auto-expand if: ≤12 total pipelines, or group has failing/running, or group has selected pipeline
            const hasFailing = items.some(p => p.status === 'failed' || p.status === 'aborted' || p.status === 'running');
            const hasSelected = items.some(p => selectedPipeline?.name === p.name);
            auto[group] = totalPipelines <= 12 || hasFailing || hasSelected || !!search;
        }
        return auto;
    }, [groups, filteredPipelines.length, manualToggles, selectedPipeline?.name, search]);

    const toggleGroup = (group: string) => {
        setManualToggles(prev => ({ ...prev, [group]: !expandedGroups[group] }));
    };

    const showSearch = pipelines.length > 3;

    return (
        <>
        {/* Mobile overlay */}
        {isOpen && <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} aria-hidden="true" />}
        <aside className={`sidebar ${isOpen ? 'sidebar-open' : ''}`} aria-label="Pipelines navigation">
            {/* Header */}
            <div className="sidebar-header">
                <span className="sidebar-title">Pipelines</span>
                <span className="pipeline-count" aria-label={`${filteredPipelines.length} pipelines`}>{filteredPipelines.length}</span>
            </div>

            {/* Search */}
            {showSearch && (
                <div className="sidebar-search">
                    <input
                        type="text"
                        placeholder="Search pipelines..."
                        aria-label="Search pipelines"
                        value={search}
                        onChange={e => onSearchChange?.(e.target.value)}
                    />
                </div>
            )}

            {/* Pipeline List */}
            <div className="pipeline-list" role="listbox" aria-label="Pipeline list">
                {loading ? (
                    <PipelineListSkeleton count={5} />
                ) : filteredPipelines.length === 0 ? (
                    <div className="empty-state">
                        <div className="empty-state-icon">{search ? <Search size={32} /> : <Inbox size={32} />}</div>
                        <div className="empty-state-title">
                            {search ? 'No matches' : 'No pipelines'}
                        </div>
                        <div className="empty-state-text">
                            {search
                                ? `No pipelines matching "${search}"`
                                : config.API_URL
                                    ? 'No pipelines registered yet. Deploy a pipeline to see it here.'
                                    : 'Configure API_URL in config.js to connect to your backend.'}
                        </div>
                    </div>
                ) : hasGroups ? (
                    (Object.entries(groups)).sort(([a], [b]) => a.localeCompare(b)).map(([group, items]) => (
                        <div key={group} className="sb-pipeline-group">
                            <div 
                                className="sb-pipeline-group-header flex items-center gap-1 px-3 py-1.5 cursor-pointer text-[11px] font-semibold uppercase text-[var(--text-muted)] tracking-wide select-none"
                                onClick={() => toggleGroup(group)}
                                role="button"
                                tabIndex={0}
                                aria-expanded={!!expandedGroups[group]}
                                aria-label={`${group === '_ungrouped' ? 'Other' : group} group, ${items.length} pipelines`}
                                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleGroup(group); } }}
                            >
                                {!expandedGroups[group] 
                                    ? <ChevronRight size={12} /> 
                                    : <ChevronDown size={12} />
                                }
                                <span>{group === '_ungrouped' ? 'Other' : group}</span>
                                <span className="ml-auto opacity-60">{items.length}</span>
                            </div>
                            {expandedGroups[group] && items.map(p => (
                                <PipelineItem
                                    key={p.name}
                                    pipeline={p}
                                    selected={selectedPipeline?.name === p.name}
                                    onClick={() => { setSelectedPipeline(p); setSidebarOpen(false); }}
                                />
                            ))}
                        </div>
                    ))
                ) : (
                    filteredPipelines.map(p => (
                        <PipelineItem
                            key={p.name}
                            pipeline={p}
                            selected={selectedPipeline?.name === p.name}
                            onClick={() => { setSelectedPipeline(p); setSidebarOpen(false); }}
                        />
                    ))
                )}
            </div>
        </aside>
        </>
    );
}

/**
 * PipelineItem - Single pipeline row in the sidebar
 */
function PipelineItem({ pipeline, selected, onClick }: PipelineItemProps) {
    const p = pipeline;
    
    return (
        <div
            className={`sb-pipeline-item ${selected ? 'active' : ''} ${p.status === 'running' ? 'running-glow' : ''}`}
            onClick={onClick}
            role="option"
            aria-selected={selected}
            aria-label={`${p.name}, status: ${p.status || 'idle'}`}
            tabIndex={0}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); } }}
        >
            {/* Icon */}
            <div className={`sb-pipeline-icon ${p.status || 'idle'}`}>
                {p.status ? <StatusIcon status={p.status} size={16} /> : <Workflow size={16} />}
            </div>

            {/* Info - takes all remaining space */}
            <div className="sb-pipeline-info">
                <div className="sb-pipeline-name" title={p.name}>{p.name}</div>
                <div className="sb-pipeline-meta">
                    <span>{p.status || 'idle'}</span>
                    <span className="sb-pipeline-schedule" title={formatSchedule(p.schedule, p.asset_schedule)}>
                        {' · '}{formatSchedule(p.schedule, p.asset_schedule)}
                    </span>
                </div>
                {p.recent_runs && p.recent_runs.length > 0 && (
                    <RunSparkline runs={p.recent_runs} />
                )}
            </div>
        </div>
    );
}

/**
 * RunSparkline - Mini bar chart of recent run results
 */
function RunSparkline({ runs }: RunSparklineProps) {
    // runs come newest-first from API, reverse so oldest is on the left
    const ordered = [...runs].reverse();
    return (
        <div className="sb-run-sparkline" title={`Last ${ordered.length} runs`}>
            {ordered.map((run, i) => (
                <div
                    key={i}
                    className={`sb-run-spark ${run.status}`}
                    title={`${run.date}: ${run.status}`}
                />
            ))}
        </div>
    );
}

/**
 * Format cron/schedule expression to human-readable
 * e.g. "cron(0 8 * * ? *)" → "daily @ 08:00"
 *      "cron(0 10 ? * MON *)" → "Mon @ 10:00"
 *      "rate(6 hours)" → "every 6h"
 */

export default PipelinesSidebar;
