'use client';

import React, { lazy, Suspense, useEffect } from 'react';
import { useClientRoute } from '@/hooks/useClientRoute';
import { Button } from '@/components/ui/button';
import { AlertTriangle, Loader2 } from './utils/icons';
import { 
    HelpModal,
    ErrorBoundary,
    CommandPalette,
    AppNav,
    Header,
    PipelinesSidebar,
    PipelineDetail
} from './components';
import { paidSurface } from '@/ee-active.generated';
import { useKeyboardShortcuts, SHORTCUTS } from './hooks';
import { usePipelinesQuery } from './hooks/queries';
import { useAppStore } from './stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { useStoreInit } from './stores/useStoreInit';
import { POLLING, viewFromPathname } from './utils';
import type { PipelineWithUI } from './types';
import { isMainView } from './types';

// Lazy load heavy components
const AllTasksView = lazy(() => import('./components/AllTasksView'));
const AllRunsView = lazy(() => import('./components/AllRunsView'));

const ViewLoader = () => (
    <div className="view-loader">
        <Loader2 size={32} className="animate-spin" />
        <span>Loading...</span>
        <div className="view-loader-bar" aria-hidden="true" />
    </div>
);

function App() {
    const router = useClientRoute();
    const pathname = router.pathname;
    const mainView = viewFromPathname(pathname);
    const BackfillsView = paidSurface.BackfillsView;
    const AssetsView = paidSurface.AssetsView;
    const BackfillModalHost = paidSurface.BackfillModalHost;

    // ========== Store ==========
    const {
        setSelectedPipeline,
        theme, toggleTheme,
        liveMode,
        showHelpModal, setShowHelpModal,
        showCommandPalette, setShowCommandPalette,
        showTaskModal, setShowTaskModal,
        backfillModalSeed, closeBackfillModal,
    } = useAppStore(useShallow(s => ({
        setSelectedPipeline: s.setSelectedPipeline,
        theme: s.theme, toggleTheme: s.toggleTheme,
        liveMode: s.liveMode,
        showHelpModal: s.showHelpModal, setShowHelpModal: s.setShowHelpModal,
        showCommandPalette: s.showCommandPalette, setShowCommandPalette: s.setShowCommandPalette,
        showTaskModal: s.showTaskModal, setShowTaskModal: s.setShowTaskModal,
        backfillModalSeed: s.backfillModalSeed,
        closeBackfillModal: s.closeBackfillModal,
    })));

    // ========== Data ==========
    const { 
        data: pipelines = [], 
        error: pipelinesError,
    } = usePipelinesQuery({ refetchInterval: liveMode ? POLLING.IDLE : false });

    const apiConnected = !pipelinesError;
    const apiError = pipelinesError ? `Cannot connect to API: ${pipelinesError.message}` : null;

    // ========== Store Init (URL sync, pipeline restore, effects) ==========
    const { navigateToExecution } = useStoreInit({ pipelines });

    // ========== Keyboard Shortcuts ==========
    useKeyboardShortcuts({
        [SHORTCUTS.HELP]: () => setShowHelpModal(true),
        [SHORTCUTS.SEARCH]: () => setShowCommandPalette(!showCommandPalette),
        [SHORTCUTS.ESCAPE]: () => {
            if (showCommandPalette) setShowCommandPalette(false);
            else if (showTaskModal) setShowTaskModal(false);
            else if (showHelpModal) setShowHelpModal(false);
            else if (backfillModalSeed) closeBackfillModal();
        },
        [SHORTCUTS.TOGGLE_THEME]: toggleTheme,
        // NOTE: d/g/c (pipeline view-modes) are owned by PipelineDetail, which
        // gates g/c on the paid surface actually being present. They were bound
        // here too — ungated — so in the OSS build 'c' set viewMode='calendar'
        // with no CalendarView to render it. That value persists (localStorage +
        // URL) and OSS hides the view-mode pills, so there was no way back; the
        // stuck 'calendar' then suppressed PipelineDetail's "No executions for
        // {date}" branch and left a bare graph. Principle #19: one surface owns
        // a key. Do not re-bind these here.
        '1': () => router.push('/pipelines/'),
        '2': () => { if (AssetsView) router.push('/assets/'); },
        '3': () => router.push('/tasks/'),
        '4': () => router.push('/runs/'),
        '5': () => { if (BackfillsView) router.push('/backfills/'); },
    });

    // ========== Render ==========
    // In the OSS build the Assets/Backfills surfaces don't exist. If one of those
    // routes is reached directly (URL, bookmark), send the user to Pipelines rather
    // than rendering a blank view.
    useEffect(() => {
        if ((mainView === 'assets' && !AssetsView) || (mainView === 'backfills' && !BackfillsView)) {
            router.replace('/pipelines/');
        }
    }, [mainView, AssetsView, BackfillsView, router]);

    return (
        <div className="app-shell">
            <Header
                apiConnected={apiConnected}
                onNotificationNavigate={(name, execId) => navigateToExecution(name, execId)}
                onNotificationNavigateBackfill={(backfillId) => router.push(`/backfills/${backfillId}/`)}
            />
            <div className="app-body">
            <AppNav />
            <div className="app">
            <a href="#main-content" className="skip-link">Skip to main content</a>
            
            {!apiConnected && (
                <div className="connection-banner" role="alert">
                    <span><AlertTriangle size={16} /> Connection lost. Retrying...</span>
                    <Button size="sm" variant="secondary" onClick={() => window.location.reload()}>Retry Now</Button>
                </div>
            )}
            
            <main className="main" id="main-content">
                {mainView === 'pipelines' ? (
                    <div className="main-top">
                        <PipelinesSidebar />
                        <PipelineDetail
                            apiError={apiError}
                            navigateToExecution={navigateToExecution}
                        />
                    </div>
                ) : mainView === 'tasks' ? (
                    <Suspense fallback={<ViewLoader />}>
                        <AllTasksView
                            onPipelineClick={(pipeline, taskDate) => {
                                navigateToExecution(pipeline.name, undefined, taskDate);
                            }}
                        />
                    </Suspense>
                ) : mainView === 'runs' ? (
                    <Suspense fallback={<ViewLoader />}>
                        <AllRunsView
                            onPipelineClick={(pipeline, run) => {
                                navigateToExecution(pipeline.name, run?.pipeline_execution, run?.date);
                            }}
                        />
                    </Suspense>
                ) : mainView === 'backfills' && BackfillsView ? (
                    <Suspense fallback={<ViewLoader />}>
                        <BackfillsView onNavigateToExecution={navigateToExecution} />
                    </Suspense>
                ) : mainView === 'assets' && AssetsView ? (
                    <ErrorBoundary fallback={
                        <div className="pd-error-fallback">
                            Failed to load Assets view. 
                            <button onClick={() => router.push('/pipelines/')}>Go to Pipelines</button>
                        </div>
                    }>
                        <Suspense fallback={<ViewLoader />}>
                            <AssetsView />
                        </Suspense>
                    </ErrorBoundary>
                ) : null}
            </main>
            
            <HelpModal isOpen={showHelpModal} onClose={() => setShowHelpModal(false)} />

            {BackfillModalHost && <BackfillModalHost />}
            
            <CommandPalette
                isOpen={showCommandPalette}
                onClose={() => setShowCommandPalette(false)}
                pipelines={pipelines}
                onSelectPipeline={(p: PipelineWithUI) => { setSelectedPipeline(p); router.push('/pipelines/'); }}
                onNavigate={(view: string) => {
                    if (view === 'help') { setShowHelpModal(true); return; }
                    if (isMainView(view)) router.push(`/${view}/`);
                }}
                onToggleTheme={toggleTheme}
                theme={theme}
            />
            </div>
            </div>
        </div>
    );
}

export { App };
export default App;
