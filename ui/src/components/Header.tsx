import React from 'react';
import { useClientRoute } from '@/hooks/useClientRoute';
import Notifications from './Notifications';
import { viewFromPathname } from '../utils';
import { useAppStore } from '../stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import {
    RefreshCw,
    Zap,
    Pause,
    ChevronRight,
    Menu,
    X
} from 'lucide-react';

interface HeaderProps {
    apiConnected: boolean;
    onNotificationNavigate: (pipelineName: string, executionId?: string) => void;
    /** ADR #68 — navigate to backfill detail from backfill notification. */
    onNotificationNavigateBackfill?: (backfillId: string) => void;
}

/**
 * Header — the app-shell topbar: contextual breadcrumbs and global controls.
 *
 * Primary navigation lives in the left rail (AppNav); this topbar carries location
 * (breadcrumbs) and global controls (API status, auto-refresh, notifications).
 * The theme toggle and account menu live in the left rail's account menu (AppNav). Top-level view is read from the pathname; other state from Zustand.
 */
export function Header({ apiConnected, onNotificationNavigate, onNotificationNavigateBackfill }: HeaderProps) {
    const { pathname } = useClientRoute();
    const mainView = viewFromPathname(pathname);

    const {
        liveMode, toggleLiveMode,
        sidebarOpen, toggleSidebar,
    } = useAppStore(useShallow(s => ({
        liveMode: s.liveMode, toggleLiveMode: s.toggleLiveMode,
        sidebarOpen: s.sidebarOpen, toggleSidebar: s.toggleSidebar,
    })));

    return (
        <header className="header" role="banner">
            {/* Mobile hamburger — only where a secondary sidebar exists */}
            {(mainView === 'pipelines' || mainView === 'assets') && (
                <button className="hdr-hamburger-btn" onClick={toggleSidebar} title="Toggle sidebar" aria-label={sidebarOpen ? 'Close sidebar' : 'Open sidebar'} aria-expanded={sidebarOpen}>
                    {sidebarOpen ? <X size={20} /> : <Menu size={20} />}
                </button>
            )}

            {/* Breadcrumbs */}
            <Breadcrumbs />

            {/* Header Controls */}
            <div className="hdr-header-controls">
                {/* API Status */}
                <div
                    className="hdr-status-badge"
                    title={apiConnected ? 'API connected' : 'API disconnected'}
                    role="status"
                    aria-label={apiConnected ? 'API connected' : 'API disconnected'}
                >
                    <div className={`status-dot ${apiConnected ? 'connected' : 'disconnected'}`} />
                    API
                </div>

                {/* Live Mode Toggle */}
                <div
                    className={`hdr-status-badge cursor-pointer ${liveMode ? 'status-badge-active' : ''}`}
                    onClick={toggleLiveMode}
                    role="button"
                    tabIndex={0}
                    aria-label={liveMode ? 'Pause auto-refresh' : 'Enable auto-refresh'}
                    aria-pressed={liveMode}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleLiveMode(); } }}
                    title={liveMode ? 'Click to pause auto-refresh' : 'Click to enable auto-refresh'}
                >
                    {liveMode ? <><RefreshCw size={14} className="hdr-auto-spin" /> Auto</> : <><Pause size={14} /> Paused</>}
                </div>

                {/* Notifications */}
                <Notifications onNavigate={onNotificationNavigate} onNavigateBackfill={onNotificationNavigateBackfill} />
            </div>
        </header>
    );
}

/**
 * Breadcrumbs — logo (home) as a separate brand element, then a clickable breadcrumb
 * trail that starts with the section name (not the logo). The last crumb is the current
 * location and is not clickable.
 */
const SECTION_LABEL: Record<string, string> = {
    pipelines: 'Pipelines',
    tasks: 'History',
    runs: 'History',
    assets: 'Assets',
    backfills: 'Backfills',
};

function Breadcrumbs() {
    const { pathname, push } = useClientRoute();
    const mainView = viewFromPathname(pathname);

    const { selectedPipeline, selectedExecution, setSelectedPipeline, setSelectedExecution, setDate } = useAppStore(useShallow(s => ({
        selectedPipeline: s.selectedPipeline, selectedExecution: s.selectedExecution,
        setSelectedPipeline: s.setSelectedPipeline, setSelectedExecution: s.setSelectedExecution,
        setDate: s.setDate,
    })));

    const latestRunDate = selectedPipeline?.recent_runs?.find(r => r.date)?.date ?? null;

    const goSection = () => { setSelectedPipeline(null); setSelectedExecution(null); push(`/${mainView}/`); };
    const goPipeline = () => { setSelectedExecution(null); if (latestRunDate) setDate(latestRunDate); };

    const sectionLabel = SECTION_LABEL[mainView] ?? mainView;
    const onPipeline = mainView === 'pipelines' && !!selectedPipeline;
    // Show the execution crumb whenever an execution is in scope — including the default
    // auto-selected latest. The short id now comes from the backend (pipeline_execution_short),
    // so it matches the history list and no longer renders a phantom like "Run hello-wo".
    const onExecution = onPipeline && !!selectedExecution;
    const key = (fn: () => void) => (e: React.KeyboardEvent) => { if (e.key === 'Enter') fn(); };

    return (
        <div className="hdr-nav-left">
            <span className="hdr-brand" aria-label="polyris">
                <span className="hdr-brand-logo"><Zap size={16} /></span>
                polyris
            </span>
            <span className="hdr-brand-divider" aria-hidden="true" />

            <nav className="hdr-breadcrumbs" aria-label="Breadcrumb">
                {onPipeline ? (
                    <span className="hdr-breadcrumb-item clickable" onClick={goSection} role="link" tabIndex={0} onKeyDown={key(goSection)}>
                        {sectionLabel}
                    </span>
                ) : (
                    <span className="hdr-breadcrumb-item hdr-breadcrumb-current" aria-current="page">{sectionLabel}</span>
                )}

                {onPipeline && (
                    <>
                        <span className="hdr-breadcrumb-sep"><ChevronRight size={14} /></span>
                        {onExecution ? (
                            <span className="hdr-breadcrumb-item clickable" onClick={goPipeline} role="link" tabIndex={0} onKeyDown={key(goPipeline)}>
                                {selectedPipeline.name}
                            </span>
                        ) : (
                            <span className="hdr-breadcrumb-item hdr-breadcrumb-current" aria-current="page">{selectedPipeline.name}</span>
                        )}
                    </>
                )}

                {onExecution && (
                    <>
                        <span className="hdr-breadcrumb-sep"><ChevronRight size={14} /></span>
                        <span className="hdr-breadcrumb-item hdr-breadcrumb-current" aria-current="page">
                            Run {selectedExecution.execution_short || selectedExecution.execution_id?.substring(0, 8)}
                        </span>
                    </>
                )}
            </nav>
        </div>
    );
}

export default Header;
