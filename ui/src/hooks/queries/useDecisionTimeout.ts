/**
 * Decision-wait timeout hook (ADR #103 1b).
 *
 * Fetches and updates the global "how long a task waits for a human decision
 * before giving up" setting. One value per deployment. GET is free; PUT is
 * Team-gated (server-side @requires; UX only for the button).
 *
 * Lives in the free `hooks/queries` package (not `ee/team/`) because the read
 * path is called from a free settings surface. See ADR #99 for the "who calls
 * it" rule for query placement.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, isOk } from '../../utils';
import { queryKeys } from '../../lib/queryClient';

const DEFAULT_SECONDS = 18000;

export interface DecisionTimeout {
    decision_timeout_seconds: number;
}

/**
 * Read the current decision timeout. Falls back to the default on any error
 * (including 404 in truly minimal deploys) so the settings page renders even
 * when the endpoint is momentarily unavailable.
 */
export function useDecisionTimeoutQuery() {
    return useQuery<DecisionTimeout>({
        queryKey: queryKeys.decisionTimeout,
        queryFn: async () => {
            const result = await api.get('/settings/decision-timeout');
            if (isOk(result) && result.data) {
                const s = (result.data as { decision_timeout_seconds?: number }).decision_timeout_seconds;
                if (typeof s === 'number') return { decision_timeout_seconds: s };
            }
            return { decision_timeout_seconds: DEFAULT_SECONDS };
        },
        staleTime: 30 * 1000,
    });
}

/**
 * Update the decision timeout. Team-only route — free tiers see 403 which the
 * caller surfaces as "Could not save". Invalidates the query on success so any
 * other reader sees the fresh value.
 */
export function useSetDecisionTimeoutMutation() {
    const qc = useQueryClient();
    return useMutation({
        mutationFn: async (seconds: number) => {
            const result = await api.put('/settings/decision-timeout', {
                decision_timeout_seconds: seconds,
            });
            if (!isOk(result)) {
                throw new Error('Save failed');
            }
            return result;
        },
        onSuccess: () => {
            qc.invalidateQueries({ queryKey: queryKeys.decisionTimeout });
        },
    });
}
