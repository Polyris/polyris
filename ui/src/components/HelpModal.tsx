import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { getApiUrl } from '@/lib/config';
import { BaseModal, ModalHeader, ModalBody, ModalFooter } from './BaseModal';
import { useKeyboardShortcuts } from '@/hooks';
import { paidSurface } from '@/ee-active.generated';
import {
    BookOpen,
    Keyboard,
    Palette,
    Plug,
    Wrench,
    Package,
    Download,
    Rocket,
    CheckCircle2,
    AlertTriangle,
    Clock,
    Zap,
    RefreshCw,
    ClipboardList,
    Settings,
    Activity,
    Bell,
    XCircle,
    Loader2,
    SkipForward,
    PlayCircle,
    Ban,
    StopCircle,
    Pause,
    Copy,
    ChevronDown,
    ChevronRight,
    ContextIcons,
} from '@/utils/icons';

/**
 * HelpModal - Help & Documentation modal
 * Contains icons legend, keyboard shortcuts, backfill docs, and API reference
 */
export function HelpModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
    // API reference (and its token-based access) is a Team feature — hidden in OSS.
    const IS_TEAM = Object.keys(paidSurface).length > 0;
    const [activeTab, setActiveTab] = useState('shortcuts');

    // Tab switching per ADR #64 (revised v0.78.5). Numeric reserved
    // for global nav; modal uses letters: s=Shortcuts, i=Icons,
    // b=Backfill, a=API. Enabled only when modal is open.
    useKeyboardShortcuts({
        's': () => setActiveTab('shortcuts'),
        'i': () => setActiveTab('icons'),
        'b': () => { if (paidSurface.BackfillsView) setActiveTab('backfill'); },
        'a': () => { if (IS_TEAM) setActiveTab('api'); },
    }, { enabled: isOpen });
    
    return (
        <BaseModal isOpen={isOpen} onClose={onClose} className="hm-help-modal">
            <ModalHeader icon={<BookOpen size={18} />} onClose={onClose}>Help & Documentation</ModalHeader>
            <div className="nav-tabs">
                <button 
                    className={`nav-tab nav-tab--sm ${activeTab === 'shortcuts' ? 'active' : ''}`}
                    onClick={() => setActiveTab('shortcuts')}
                >
                    <Keyboard size={14} /> Shortcuts
                </button>
                <button 
                    className={`nav-tab nav-tab--sm ${activeTab === 'icons' ? 'active' : ''}`}
                    onClick={() => setActiveTab('icons')}
                >
                    <Palette size={14} /> Icons
                </button>
                {paidSurface.BackfillsView && (
                    <button 
                        className={`nav-tab nav-tab--sm ${activeTab === 'backfill' ? 'active' : ''}`}
                        onClick={() => setActiveTab('backfill')}
                    >
                        <ContextIcons.backfill size={14} /> Backfill
                    </button>
                )}
                {IS_TEAM && (
                    <button 
                        className={`nav-tab nav-tab--sm ${activeTab === 'api' ? 'active' : ''}`}
                        onClick={() => setActiveTab('api')}
                    >
                        <Plug size={14} /> API
                    </button>
                )}
            </div>
            <ModalBody>
                {activeTab === 'icons' ? (
                    <IconsLegendTab />
                ) : activeTab === 'backfill' ? (
                    <BackfillDocsTab />
                ) : activeTab === 'api' && IS_TEAM ? (
                    <ApiReferenceTab />
                ) : (
                    <KeyboardShortcutsTab />
                )}
            </ModalBody>
            <ModalFooter>
                <Button onClick={onClose}>Got it!</Button>
            </ModalFooter>
        </BaseModal>
    );
}

function KeyboardShortcutsTab() {
    // Grouped by surface type per ADR #64 / CLAUDE.md #19. Keep in sync
    // with what's actually wired — see useKeyboardShortcuts hook usage
    // across components.
    //
    // v0.78.5: numeric keys reserved for top-level nav; inner surfaces
    // use letter keys matching first letter of tab name.
    //
    // A shortcut listed here but not wired in this build is a documentation bug
    // (#19). Gantt/Calendar were already filtered on the paid surface; the
    // Backfills/Assets groups were not, so the OSS build advertised ~14 keys
    // that do nothing. Everything paid is filtered the same way now — the
    // KeyboardShortcutsTab guard test pins it.
    const IS_TEAM = Object.keys(paidSurface).length > 0;
    const HAS_PAID_DETAIL_PAGES = Boolean(paidSurface.BackfillsView || paidSurface.AssetsView);
    const groups: Array<{ title: string; items: Array<{ key: string; action: string }> }> = [
        {
            title: 'Global',
            items: [
                { key: '?', action: 'Show this help' },
                { key: '⌘K', action: 'Quick search (Command Palette)' },
                { key: 'Esc', action: 'Close modal / dismiss overlay' },
                { key: '⌘⇧T', action: 'Toggle dark / light theme' },
            ],
        },
        {
            title: 'Top-level navigation (numeric keys reserved)',
            items: [
                { key: '1', action: 'Pipelines view' },
                // 2 / 5 route to paid views — App.tsx no-ops them when the slot
                // is empty, so they are only listed when actually wired.
                ...(paidSurface.AssetsView ? [{ key: '2', action: 'Assets view' }] : []),
                { key: '3', action: 'History (Tasks)' },
                { key: '4', action: 'History (Runs)' },
                ...(paidSurface.BackfillsView ? [{ key: '5', action: 'Backfills view' }] : []),
            ],
        },
        {
            title: 'List views',
            items: [
                { key: '⌘R', action: 'Refresh data (all list views)' },
                // Row navigation is wired on the Backfills list (paid surface).
                ...(paidSurface.BackfillsView ? [
                    { key: '/', action: 'Focus the search input (Backfills, History)' },
                    { key: 'J', action: 'Highlight next row (Backfills list)' },
                    { key: 'K', action: 'Highlight previous row (Backfills list)' },
                    { key: 'Enter', action: 'Open highlighted row (Backfills list)' },
                ] : [{ key: '/', action: 'Focus the search input (History)' }]),
            ],
        },
        {
            title: HAS_PAID_DETAIL_PAGES
                ? 'Detail pages (Backfill, Asset, Pipeline)'
                : 'Detail pages (Pipeline)',
            items: [
                { key: '⌘R', action: 'Refresh data' },
                { key: 'Esc', action: 'Go back to list' },
            ],
        },
        {
            title: 'Pipeline view modes',
            items: [
                { key: 'D', action: 'Switch to DAG view' },
                // Gantt & Calendar are paid view-modes — only listed when present.
                ...(paidSurface.GanttChart ? [{ key: 'G', action: 'Switch to Gantt view' }] : []),
                ...(paidSurface.CalendarView ? [{ key: 'C', action: 'Switch to Calendar view' }] : []),
            ],
        },
        ...(paidSurface.AssetsView ? [{
            title: 'Asset detail tabs',
            items: [
                { key: 'O', action: 'Overview tab' },
                { key: 'S', action: 'Schema tab' },
                { key: 'P', action: 'Partitions tab' },
                { key: 'E', action: 'Events tab' },
                { key: 'C', action: 'Checks tab' },
                { key: 'L', action: 'Lineage tab' },
            ],
        }] : []),
        {
            title: 'Task Detail modal tabs',
            items: [
                { key: 'D', action: 'Details tab' },
                { key: 'T', action: 'Timeline tab' },
                { key: 'A', action: 'Actions tab' },
            ],
        },
        {
            title: 'Help modal tabs',
            items: [
                { key: 'S', action: 'Shortcuts tab' },
                { key: 'I', action: 'Icons tab' },
                // Mirrors the tab buttons above, which are gated the same way.
                ...(paidSurface.BackfillsView ? [{ key: 'B', action: 'Backfill tab' }] : []),
                ...(IS_TEAM ? [{ key: 'A', action: 'API tab' }] : []),
            ],
        },
        {
            title: 'Modals with primary action',
            items: [
                { key: 'Esc', action: 'Close without submit' },
                { key: '⌘↵', action: paidSurface.BackfillsView
                    ? 'Submit / Start (e.g. start backfill)'
                    : 'Submit the primary action' },
            ],
        },
    ];

    return (
        <div className="hm-help-section">
            <h4>Keyboard Shortcuts</h4>
            <p className="hm-help-note">
                Shortcuts are grouped by where they apply. ⌘ = Ctrl on Linux/Windows.
            </p>
            {groups.map((group) => (
                <div key={group.title} className="hm-shortcut-group">
                    <h5 className="hm-shortcut-group-title">{group.title}</h5>
                    <div className="hm-shortcuts-list">
                        {group.items.map(({ key, action }) => (
                            <div key={`${group.title}-${key}`} className="hm-shortcut-row">
                                <kbd className="hm-shortcut-kbd">{key}</kbd>
                                <span className="hm-shortcut-desc">{action}</span>
                            </div>
                        ))}
                    </div>
                </div>
            ))}
        </div>
    );
}

function IconsLegendTab() {
    return (
        <>
            <div className="hm-help-section">
                <h4>Element Types</h4>
                <div className="hm-help-grid">
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Wrench size={20} /></span>
                        <div>
                            <strong>Task</strong>
                            <p>A processing unit in a pipeline. Can be Lambda, Glue, ECS, etc.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Package size={20} /></span>
                        <div>
                            <strong>Asset</strong>
                            <p>A data artifact (table, file, dataset) produced or consumed by tasks.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Wrench size={20} className="text-blue-500" /></span>
                        <div>
                            <strong>Producer</strong>
                            <p>A task that creates/updates an asset (outlets).</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Download size={20} /></span>
                        <div>
                            <strong>Consumer</strong>
                            <p>A task that reads/uses an asset (inlets).</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Rocket size={20} /></span>
                        <div>
                            <strong>DAG Trigger</strong>
                            <p>A pipeline triggered when required assets are ready.</p>
                        </div>
                    </div>
                </div>
            </div>
            
            <div className="hm-help-section">
                <h4>Task Statuses</h4>
                <div className="hm-help-grid">
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><CheckCircle2 size={20} className="text-green-500" /></span>
                        <div>
                            <strong>Success</strong>
                            <p>Task completed successfully.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Loader2 size={20} className="text-blue-500" /></span>
                        <div>
                            <strong>Running</strong>
                            <p>Task is currently executing.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Loader2 size={20} className="text-blue-400" /></span>
                        <div>
                            <strong>Pending</strong>
                            <p>Task queued — about to start.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><XCircle size={20} className="text-red-500" /></span>
                        <div>
                            <strong>Failed</strong>
                            <p>Task failed with an error.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><AlertTriangle size={20} className="text-red-400" /></span>
                        <div>
                            <strong>Upstream Failed</strong>
                            <p>Task skipped because an upstream dependency failed.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Clock size={20} className="text-gray-400" /></span>
                        <div>
                            <strong>Waiting</strong>
                            <p>Task waiting for upstream dependencies.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><SkipForward size={20} className="text-slate-500" /></span>
                        <div>
                            <strong>Skipped</strong>
                            <p>Task was skipped (trigger rule or manual).</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><PlayCircle size={20} className="text-indigo-500" /></span>
                        <div>
                            <strong>Deps Ready</strong>
                            <p>Dependencies complete, ready to run.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Clock size={20} className="text-amber-500" /></span>
                        <div>
                            <strong>Waiting Delay</strong>
                            <p>Waiting for scheduled delay (wait_before).</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Pause size={20} className="text-amber-500" /></span>
                        <div>
                            <strong>Paused</strong>
                            <p>Waiting for manual approval.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><StopCircle size={20} className="text-amber-600" /></span>
                        <div>
                            <strong>Stopped</strong>
                            <p>Task was stopped manually.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Ban size={20} className="text-orange-500" /></span>
                        <div>
                            <strong>Aborted</strong>
                            <p>Task was aborted due to pipeline stop.</p>
                        </div>
                    </div>
                </div>
            </div>
            
            <div className="hm-help-section">
                <h4>Asset Staleness</h4>
                <div className="hm-help-grid">
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><CheckCircle2 size={20} className="text-green-500" /></span>
                        <div>
                            <strong>Fresh</strong>
                            <p>Asset updated within last 24 hours.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><AlertTriangle size={20} className="text-amber-500" /></span>
                        <div>
                            <strong>Warning</strong>
                            <p>Asset updated 24-48 hours ago.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Clock size={20} className="text-red-500" /></span>
                        <div>
                            <strong>Stale</strong>
                            <p>Asset not updated for more than 48 hours.</p>
                        </div>
                    </div>
                </div>
            </div>
        </>
    );
}

function BackfillDocsTab() {
    return (
        <div className="hm-backfill-docs">
            <div className="hm-help-section">
                <h4>What is Backfill?</h4>
                <p>Backfill rebuilds a pipeline or asset across a range of partitions (typically dates).
                One backfill = one persisted record + one orchestrator SFN + N child executions
                (one per partition). Track progress at <code>/backfills/</code>.</p>
            </div>

            <div className="hm-help-section">
                <h4>How to start a backfill</h4>
                <div className="hm-help-grid">
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Wrench size={20} /></span>
                        <div>
                            <strong>From a Pipeline</strong>
                            <p>Pipeline Detail → <em>Backfill</em> button. Modal opens with the
                            pipeline pre-selected. Pick date range, optional task subset, click Start.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Package size={20} /></span>
                        <div>
                            <strong>From an Asset</strong>
                            <p>Asset Detail → <em>Backfill</em> button. The producer pipeline is
                            auto-resolved from outlets. Choose downstream strategy (auto / all / none)
                            for downstream consumers.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Zap size={20} /></span>
                        <div>
                            <strong>From a Matrix Cell</strong>
                            <p>Click any missing/failed/queued cell in the Asset Matrix. Modal
                            opens pre-filled with that exact partition and asset target.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><ContextIcons.backfill size={20} /></span>
                        <div>
                            <strong>From a Task</strong>
                            <p>Task Detail Modal → <em>Backfill This Task</em>. Modal opens with
                            the task pre-selected as the subset to run.</p>
                        </div>
                    </div>
                </div>
            </div>

            <div className="hm-help-section">
                <h4>Options</h4>
                <div className="hm-help-grid">
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><Zap size={20} /></span>
                        <div>
                            <strong>Skip Completed (default on)</strong>
                            <p>Partitions that already succeeded are short-circuited at SFN-iteration
                            time. Saves cost on re-runs.</p>
                        </div>
                    </div>
                    <div className="hm-help-item">
                        <span className="hm-help-icon"><RefreshCw size={20} /></span>
                        <div>
                            <strong>Force / Re-run All</strong>
                            <p>Turn off <em>Skip Completed</em> to re-process every partition
                            regardless of prior outcome. Use when logic changed.</p>
                        </div>
                    </div>
                </div>
            </div>

            <div className="hm-help-section">
                <h4>Cascade (asset target only)</h4>
                <ul className="text-sm leading-relaxed">
                    <li><strong>auto</strong> — Downstream consumers fire only if their trigger
                    rules allow (default and safest).</li>
                    <li><strong>all</strong> — Every consumer is force-triggered regardless of
                    trigger rule.</li>
                    <li><strong>none</strong> — Only rebuild this asset; no downstream propagation.</li>
                </ul>
            </div>

            <div className="hm-help-section">
                <h4>Task variables (available in every child execution)</h4>
                <div className="hm-api-endpoints mt-2">
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">current_date</code>
                        <span className="hm-api-desc">2025-01-15 (daily anchor)</span>
                    </div>
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">partition_key</code>
                        <span className="hm-api-desc">granularity-aware (YYYY-MM-DD, YYYY-Www, YYYY-MM, YYYY-MM-DDTHH)</span>
                    </div>
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">backfill_id</code>
                        <span className="hm-api-desc">bf-a1b2c3d4 (or empty if not in a backfill)</span>
                    </div>
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">is_backfill</code>
                        <span className="hm-api-desc">true / false</span>
                    </div>
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">year / month / day</code>
                        <span className="hm-api-desc">2025 / 01 / 15</span>
                    </div>
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">previous_date / next_date</code>
                        <span className="hm-api-desc">2025-01-14 / 2025-01-16</span>
                    </div>
                    <div className="hm-api-endpoint">
                        <code className="hm-api-method get">minus_7_days / minus_14_days / minus_30_days</code>
                        <span className="hm-api-desc">2025-01-08 / 2025-01-01 / 2024-12-16</span>
                    </div>
                </div>
            </div>

            <div className="hm-help-section">
                <h4>Tracking + control</h4>
                <ul className="text-sm leading-relaxed">
                    <li><strong>Backfills page</strong> — <code>/backfills/</code> lists recent
                    backfills with status filter chips and progress bars.</li>
                    <li><strong>Detail page</strong> — <code>/backfills/{'{id}'}/</code> shows
                    partition heatmap (per-partition status), child executions table, summary cards.</li>
                    <li><strong>Cancel</strong> — cooperative: status flips to <em>canceled</em>;
                    in-flight children complete, remaining partitions short-circuit.</li>
                    <li><strong>Retry-failed</strong> — forks a new backfill with only the failed
                    partitions, linked via <code>parent_backfill_id</code>.</li>
                </ul>
            </div>

            <div className="hm-help-section">
                <h4>Limits</h4>
                <ul className="text-sm leading-relaxed">
                    <li><strong>Hard limit</strong>: 1000 partitions per backfill (rejected at 400)</li>
                    <li><strong>Soft limit</strong>: 500 partitions (preview shows warning)</li>
                    <li><strong>Max parallel</strong>: 10 concurrent partitions (default 5)</li>
                </ul>
            </div>

            <div className="hm-help-section">
                <h4>Example Scenarios</h4>
                <div className="text-sm leading-relaxed">
                    <p><strong>Bug fix in task_C:</strong> Open Task Detail Modal for task_C → Backfill This Task → date range.</p>
                    <p><strong>New data source:</strong> Asset Detail → Backfill, downstream=all to force consumers.</p>
                    <p><strong>Failed partition:</strong> Backfill detail page → Retry Failed.</p>
                    <p><strong>Single missing day:</strong> Asset Matrix → click the red/missing cell.</p>
                </div>
            </div>
        </div>
    );
}

/** Copyable code block */
function CopyBlock({ children }: { children: React.ReactNode }) {
    const [copied, setCopied] = useState(false);
    const handleCopy = () => {
        navigator.clipboard.writeText(String(children)).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        });
    };
    return (
        <div className="hm-api-copy-block">
            <pre><code>{children}</code></pre>
            <button className="hm-api-copy-btn" onClick={handleCopy} title="Copy" aria-label={copied ? "Copied" : "Copy to clipboard"}>
                {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
            </button>
        </div>
    );
}

/**
 * Every /api/* endpoint requires an `Authorization` header once `AUTH_ENABLED`
 * is on (ADR #65). The examples below are authored WITHOUT it and the header is
 * injected centrally here, so it is shown on all ~25 of them and can never drift
 * out of sync across copies (Principle #1). Examples reference `$API_TOKEN` —
 * the user exports it once (`export API_TOKEN=plrs_…`) so every example is
 * copy-paste runnable. Idempotent: skips injection if an Authorization header is
 * already present, and leaves non-curl strings untouched.
 */
export const AUTH_CURL_HEADER = '-H "Authorization: Bearer $API_TOKEN"';
export function withAuthHeader(curlCmd: string): string {
    if (!curlCmd.startsWith('curl') || /Authorization:/i.test(curlCmd)) return curlCmd;
    return curlCmd.replace(/^curl(\s+-X\s+\w+)?/, (m) => `${m} ${AUTH_CURL_HEADER}`);
}

/** Collapsible API endpoint with example */
function ApiEndpointDetail({ method, path, desc, example, response = '' }: { method: string; path: string; desc: string; example: string; response?: string }) {
    const [open, setOpen] = useState(false);
    const methodClass = method === 'GET' ? 'get' : method === 'POST' ? 'post' : method === 'PUT' ? 'put' : 'delete';
    const hasDetails = example || response;
    return (
        <div className="hm-api-endpoint-detail">
            <div className="hm-api-endpoint" onClick={() => hasDetails && setOpen(!open)} style={{ cursor: hasDetails ? 'pointer' : 'default' }}>
                <code className={`hm-api-method ${methodClass}`}>{method}</code>
                <code className="hm-api-path">{path}</code>
                <span className="hm-api-desc">{desc}</span>
                {hasDetails && (
                    <span className="hm-api-chevron">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
                )}
            </div>
            {open && (
                <div className="hm-api-example">
                    {example && <CopyBlock>{withAuthHeader(example)}</CopyBlock>}
                    {response && (
                        <div className="hm-api-response">
                            <span className="hm-api-response-label">Response:</span>
                            <CopyBlock>{response}</CopyBlock>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

function ApiReferenceTab() {
    // Real deployed base, e.g. https://abc123.execute-api.us-east-1.amazonaws.com/dev
    // (getApiUrl() returns ".../dev/api"; strip the trailing /api so the per-endpoint
    // examples below — which append "/api/<path>" — produce the correct full URL with
    // the stage). Falls back to a placeholder (with /dev) when config isn't loaded.
    const configured = (getApiUrl() || '').replace(/\/api\/?$/, '');
    const BASE = configured || 'https://{api-id}.execute-api.us-east-1.amazonaws.com/dev';
    const isPlaceholder = !configured;
    
    return (
        <div className="hm-api-docs">
            <div className="hm-help-section mb-4">
                <h4>Base URL</h4>
                <CopyBlock>{BASE}</CopyBlock>
                <p className="hm-help-note mt-2">
                    {isPlaceholder && <>Replace <code>{'{api-id}'}</code> with your API Gateway ID. </>}
                    The <code>/dev</code> stage is part of the URL. An{' '}
                    <code>Authorization</code> header is required only when the API runs
                    with <code>AUTH_ENABLED=true</code>; the examples use{' '}
                    <code>$API_TOKEN</code>, so export it once:{' '}
                    <code>export API_TOKEN=…</code>.{' '}
                    {paidSurface.ApiTokensSection
                        ? <>Create a PAT (<code>plrs_…</code>) under your menu → Settings → API Tokens, or use a Cognito access token.</>
                        : <>Use a Cognito access token.</>}
                    {' '}Click any endpoint to see a full curl example.
                </p>
            </div>
            
            <div className="hm-api-section">
                <h4><ClipboardList size={16} className="inline mr-1" /> Pipelines</h4>
                <div className="hm-api-endpoints">
                    <ApiEndpointDetail method="GET" path="/api/pipelines" desc="List all pipelines with status"
                        example={`curl ${BASE}/api/pipelines`}
                        response={`[{"name": "acme-daily", "status": "success", "group": "acme", ...}]`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/pipeline-status?name=X&date=Y" desc="Get tasks and status for a pipeline"
                        example={`curl "${BASE}/api/pipeline-status?name=acme-daily&date=2026-02-17"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/pipeline-dag?name=X" desc="Get DAG structure (nodes + edges)"
                        example={`curl "${BASE}/api/pipeline-dag?name=acme-daily"`}
                        response={`{"name": "acme-daily", "nodes": [...], "edges": [{"from": "extract_listings", "to": "stage_listings"}]}`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/pipeline-executions?name=X&date=Y" desc="List SFN executions for date"
                        example={`curl "${BASE}/api/pipeline-executions?name=acme-daily&date=2026-02-17"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/pipeline-logs?name=X" desc="Get recent pipeline logs"
                        example={`curl "${BASE}/api/pipeline-logs?name=acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/pipeline-run?name=X" desc="Trigger a pipeline run"
                        example={`curl -X POST "${BASE}/api/pipeline-run?name=acme-daily" \\\n  -H "Content-Type: application/json" \\\n  -d '{"input": {"current_date": "2026-02-17"}}'`}
                        response={`{"execution_arn": "arn:aws:states:...", "started_at": "2026-02-17T..."}`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/pipeline-run (partial)" desc="Run specific tasks, skip others"
                        example={`curl -X POST "${BASE}/api/pipeline-run?name=acme-daily" \\\n  -H "Content-Type: application/json" \\\n  -d '{"input": {"current_date": "2026-02-17", "skip_tasks": ["extract_listings", "extract_catalog"]}}'`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/pipeline-restart?name=X" desc="Restart entire pipeline for today"
                        example={`curl -X POST "${BASE}/api/pipeline-restart?name=acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/pipeline-pause?name=X" desc="Pause/unpause a pipeline"
                        example={`curl -X POST "${BASE}/api/pipeline-pause?name=acme-daily"`}
                        response={`{"paused": true}`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/backfill" desc="Unified backfill — pipeline or asset target, single partition or range (replaces /pipeline-force-trigger, /pipeline-backfill, /assets/backfill)"
                        example={`curl -X POST "${BASE}/api/backfill" \\\n  -H "Content-Type: application/json" \\\n  -d '{"target": {"type": "pipeline", "name": "acme-daily"}, "partitions": {"start": "2026-02-10", "end": "2026-02-17"}, "options": {"max_parallel": 5, "skip_completed": true}}'`}
                        response={`{"backfill_id": "bf-a3f2c91d", "target_pipeline": "acme-daily", "partition_count_to_run": 8}`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/backfills?status=active" desc="List backfills, optionally filtered"
                        example={`curl "${BASE}/api/backfills?status=active"`}
                    />
                </div>
            </div>
            
            <div className="hm-api-section">
                <h4><Settings size={16} className="inline mr-1" /> Tasks</h4>
                <div className="hm-api-endpoints">
                    <ApiEndpointDetail method="GET" path="/api/tasks?status=X&date=Y&pipeline=Z" desc="List all tasks with filters"
                        example={`curl "${BASE}/api/tasks?status=failed&date=2026-02-17"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/task-events?name=X" desc="Get task events timeline"
                        example={`curl "${BASE}/api/task-events?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/task-config?name=X" desc="Get task configuration"
                        example={`curl "${BASE}/api/task-config?name=stage_listings"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/task-output?name=X&date=Y" desc="Get a task's stored input and output"
                        example={`curl "${BASE}/api/task-output?name=stage_listings&date=2026-02-17"`} />
                    <ApiEndpointDetail method="PUT" path="/api/task-config?name=X" desc="Update task configuration"
                        example={`curl -X PUT "${BASE}/api/task-config?name=stage_listings" \\\n  -H "Content-Type: application/json" \\\n  -d '{"retries": 3, "retry_delay": 60}'`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/task-restart?name=X" desc="Restart a specific task"
                        example={`curl -X POST "${BASE}/api/task-restart?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/task-skip?name=X" desc="Skip a waiting/failed task"
                        example={`curl -X POST "${BASE}/api/task-skip?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/task-success?name=X" desc="Mark task as success"
                        example={`curl -X POST "${BASE}/api/task-success?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/task-fail?name=X" desc="Mark task as failed"
                        example={`curl -X POST "${BASE}/api/task-fail?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/task-stop?name=X" desc="Stop a running task"
                        example={`curl -X POST "${BASE}/api/task-stop?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/task-retry?name=X" desc="Retry a failed task"
                        example={`curl -X POST "${BASE}/api/task-retry?name=stage_listings-2026-02-17-acme-daily"`}
                    />
                </div>
            </div>
            
            <div className="hm-api-section">
                <h4><Activity size={16} className="inline mr-1" /> Executions</h4>
                <div className="hm-api-endpoints">
                    <ApiEndpointDetail method="GET" path="/api/runs?date=Y&pipeline=Z&status=X" desc="List all runs with filters"
                        example={`curl "${BASE}/api/runs?date=2026-02-17&pipeline=acme-daily"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/execution-stop?id=ARN" desc="Stop a running execution"
                        example={`curl -X POST "${BASE}/api/execution-stop?id=arn:aws:states:us-east-1:123:execution:..."`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/execution-pause?id=ARN" desc="Pause a running execution"
                        example={`curl -X POST "${BASE}/api/execution-pause?id=arn:aws:states:us-east-1:123:execution:..."`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/execution-resume?id=ARN" desc="Resume a paused execution"
                        example={`curl -X POST "${BASE}/api/execution-resume?id=arn:aws:states:us-east-1:123:execution:..."`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/execution-children?id=ARN" desc="Get child executions (wrappers)"
                        example={`curl "${BASE}/api/execution-children?id=arn:aws:states:us-east-1:123:execution:..."`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/execution-parent?id=ARN" desc="Get parent pipeline execution"
                        example={`curl "${BASE}/api/execution-parent?id=arn:aws:states:us-east-1:123:execution:..."`}
                    />
                </div>
            </div>
            
            <div className="hm-api-section">
                <h4><Package size={16} className="inline mr-1" /> Assets</h4>
                <div className="hm-api-endpoints">
                    <ApiEndpointDetail method="GET" path="/api/assets" desc="List all assets with staleness"
                        example={`curl "${BASE}/api/assets"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/asset-events?name=X&limit=N" desc="Get asset update events"
                        example={`curl "${BASE}/api/asset-events?name=target_pdp_pond&limit=10"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/assets/lineage" desc="Get cross-pipeline asset lineage"
                        example={`curl "${BASE}/api/assets/lineage"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/assets/queued" desc="Get queued asset events"
                        example={`curl "${BASE}/api/assets/queued"`}
                    />
                    <ApiEndpointDetail method="POST" path="/api/asset-trigger?name=X" desc="Manually trigger an asset event"
                        example={`curl -X POST "${BASE}/api/asset-trigger?name=target_pdp_pond"`}
                    />
                    <ApiEndpointDetail method="DELETE" path="/api/asset-delete?name=X" desc="Delete an asset"
                        example={`curl -X DELETE "${BASE}/api/asset-delete?name=target_pdp_pond"`}
                    />
                </div>
            </div>

            <div className="hm-api-section">
                <h4><Bell size={16} className="inline mr-1" /> Other</h4>
                <div className="hm-api-endpoints">
                    <ApiEndpointDetail method="GET" path="/api/notifications?hours=24" desc="Get recent failures & alerts"
                        example={`curl "${BASE}/api/notifications?hours=24"`}
                    />
                    <ApiEndpointDetail method="GET" path="/api/health" desc="Health check"
                        example={`curl "${BASE}/api/health"`}
                        response={`{"status": "ok"}`}
                    />
                </div>
            </div>
        </div>
    );
}
