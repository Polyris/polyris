// Pipeline queries and mutations
export {
    usePipelinesQuery,
    usePipelinesStatsIncomplete,
    usePipelineDetailQuery,
    usePipelineExecutionsQuery,
    usePipelineRunsQuery,
    useCalendarExecutionsQuery,
    usePipelineTasksList,
} from './usePipelineQueries';
export type { PipelineTaskRef } from './usePipelineQueries';

// Global data queries
export {
    useAllTasksQuery,
    useAllRunsQuery,
    useTaskEventsQuery,
    useNotificationsQuery,
} from './useGlobalQueries';

// /api/health as the System status in the header.
export { useSystemStatusQuery, SYSTEM_STATUS_POLL_MINUTES } from './useSystemStatusQuery';
export type { SystemFinding, SystemStatusState } from './useSystemStatusQuery';

// Asset queries/types moved to src/ee/team/hooks/queries (Team-tier) — ADR #99.

// Backfill queries and mutations (v0.78+, ADR #51)
export {
    useBackfillsListQuery,
} from './useBackfillQueries';

// Deployment entitlement: tier + can() gate for Enterprise UI (ADR #100).
export {
    useCapabilitiesQuery,
    useCan,
    useTier,
} from './useCapabilities';
export type { Capabilities } from './useCapabilities';
