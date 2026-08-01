/**
 * Backfill list query (v0.78+, per ADR #51).
 *
 * GET /api/backfills powers the Header notification badge and the Team backfills
 * view. The Team-tier start / preview / detail / cancel / retry hooks moved to
 * src/ee/team/hooks/queries/useBackfillQueries.ts (ADR #99); they are consumed only
 * by Team components and stripped from the OSS build.
 */

import { useQuery } from '@tanstack/react-query';
import { api } from '../../utils';
import { queryKeys } from '../../lib/queryClient';
import type { BackfillSummary, BackfillStatus } from '../../types';

const FIVE_SECONDS = 5_000;
const THIRTY_SECONDS = 30_000;

/**
 * useBackfillsListQuery — GET /api/backfills
 *
 * Lists recent Backfills, optionally filtered by status.
 * Polls every 30s when active filter is set, every 5s when 'active'.
 */
export function useBackfillsListQuery(statusFilter: BackfillStatus | 'active' | null = null) {
    return useQuery({
        queryKey: queryKeys.backfillsList(statusFilter),
        queryFn: async (): Promise<BackfillSummary[]> => {
            const params = statusFilter ? `?status=${statusFilter}` : '';
            const result = await api.get(`/backfills${params}`);
            if (!result.ok) {
                throw new Error(result.error || 'Failed to load backfills');
            }
            const data = result.data as { backfills?: BackfillSummary[]; count?: number };
            return data.backfills || [];
        },
        refetchInterval: statusFilter === 'active' ? FIVE_SECONDS : THIRTY_SECONDS,
    });
}
