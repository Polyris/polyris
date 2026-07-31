// Pipeline queries and mutations
export {
    usePipelinesQuery,
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
