// Status icons - USE StatusIcon component from utils/icons.jsx instead
// The emoji constants below are DEPRECATED and kept only for reference
// Import { StatusIcon } from '@/utils/icons' for consistent icon rendering

// Legacy STATUS_ICONS - DEPRECATED, use StatusIcon component instead
// export const STATUS_ICONS = { ... };

// Legacy STALENESS_ICONS - DEPRECATED, use StalenessIcon component instead  
// export const STALENESS_ICONS = { ... };

// Status colors for badges
export const STATUS_COLORS = {
    waiting: 'waiting',
    waiting_paused: 'warning',
    waiting_decision: 'info',
    deps_ready: 'running',
    running: 'running',
    pending: 'running',
    success: 'success',
    succeeded: 'success',
    failed: 'error',
    upstream_failed: 'error',
    skipped: 'skipped',
    stopped: 'stopped',
    waiting_delay: 'warning',
    partial: 'warning',
    aborted: 'aborted',
    timed_out: 'error',
    recovered: 'warning',
    // Asset statuses
    updated: 'success',
    ready: 'success',
    listening: 'info',
    watching: 'info',
    queued: 'muted',
    up_failed: 'error'
};

// View modes for pipeline detail
export const VIEW_MODES = {
    DAG: 'dag',
    GANTT: 'gantt',
    TABLE: 'table',
    CALENDAR: 'calendar'
};

// Task status categories
// Task status values + terminal set are the SINGLE generated source
// (polyris/constants.py → generated/enums.ts, gated by check-generate-enums).
// Re-exported here so existing importers keep working; this file no longer
// hand-maintains the status vocabulary (ADR #93).
import { TASK_STATUS, TASK_TERMINAL_STATUSES } from '@/generated/enums';
export { TASK_STATUS };

// Terminal statuses - task is done, won't change
// Note: STOPPED is NOT terminal - task can be restarted
export const TERMINAL_STATUSES = new Set<string>(TASK_TERMINAL_STATUSES);

// Note: success/failure/waiting/active/countdown status groupings are canonical in
// src/generated/enums.ts (TASK_*_STATUSES). Do not re-declare them here.

// Helper functions
export const isTerminalStatus = (status: string) => TERMINAL_STATUSES.has(status);

// "Why waiting" reasons for tooltip
export const getWaitingReason = (task: { status?: string; dependencies?: string[]; wait_for?: string | Array<{ asset_name?: string; name?: string }>; wait_before?: number } | null): string | null => {
    if (!task) return null;
    
    const status = task.status;
    
    // Parse wait_for if present
    let waitForAssets = [];
    if (task.wait_for) {
        try {
            const parsed = typeof task.wait_for === 'string' 
                ? JSON.parse(task.wait_for) 
                : task.wait_for;
            if (Array.isArray(parsed)) {
                waitForAssets = parsed.map(a => a.asset_name || a.name).filter(Boolean);
            }
        } catch {
            // ignore parse errors
        }
    }
    
    switch (status) {
        case TASK_STATUS.WAITING:
            if ((task.dependencies?.length ?? 0) > 0 && waitForAssets.length > 0) {
                return `Waiting for tasks: ${task.dependencies!.join(', ')} and assets: ${waitForAssets.join(', ')}`;
            }
            if ((task.dependencies?.length ?? 0) > 0) {
                return `Waiting for: ${task.dependencies!.join(', ')}`;
            }
            if (waitForAssets.length > 0) {
                return `Waiting for assets: ${waitForAssets.join(', ')}`;
            }
            return 'Waiting for dependencies';
            
        case TASK_STATUS.DEPS_READY:
            if ((task.wait_before ?? 0) > 0) {
                return 'Dependencies ready, countdown starting...';
            }
            return 'Dependencies ready, starting soon...';
            
        case TASK_STATUS.WAITING_DELAY:
            return 'Countdown in progress';
            
        case TASK_STATUS.WAITING_PAUSED:
            return 'Pipeline is paused';
            
        case TASK_STATUS.WAITING_DECISION:
            return 'Waiting for manual decision';
            
        case TASK_STATUS.RUNNING:
            return 'Task is executing';
            
        case TASK_STATUS.PENDING:
            return 'Pending redrive';
            
        default:
            return null;
    }
};

// Time constants (in ms)
export const MS = {
    SECOND: 1000,
    MINUTE: 60 * 1000,
    HOUR: 60 * 60 * 1000,
    DAY: 24 * 60 * 60 * 1000,
    TICK_INTERVAL: 1000,       // UI tick refresh
};

// Polling intervals (in ms)
export const POLLING = {
    ACTIVE: 15000,    // 15s when tasks are actively running/countdown
    IDLE: 30000,      // 30s when pipeline is idle
    ASSETS: 30000,    // 30s for assets view refresh
    TICK: 2000        // 2s for countdown tick
};

// API configuration
export const API = {
    BASE_URL: '',
    TIMEOUT: 30000
};

// UI configuration
export const UI = {
    TOAST_DURATION: 4000,
    DEBOUNCE_DELAY: 300,
    MAX_EVENTS_DISPLAY: 100,
    MAX_TAGS_DISPLAY: 3,
    SEARCH_MIN_CHARS: 2
};

// Staleness thresholds (in hours)
export const STALENESS = {
    FRESH_HOURS: 24,
    WARNING_HOURS: 48,
    STALE_HOURS: 72
};

// Graph layout configuration
export const GRAPH = {
    NODE_WIDTH: 160,
    NODE_HEIGHT: 70,
    NODE_SEP: 60,
    RANK_SEP: 100,
    MARGIN: 30
};

// Keyboard shortcuts
export const KEYS = {
    HELP: '?',
    SEARCH: 'k',
    ESCAPE: 'Escape',
    TOGGLE_THEME: '\\',
    REFRESH: 'r',
    DAG_VIEW: 'd',
    GANTT_VIEW: 'g',
    TABLE_VIEW: 't'
};
