/**
 * Deployment capabilities + the `can()` gate (ADR #100).
 *
 * Fetches the paid surface this deployment is entitled to (`GET /api/capabilities`)
 * and exposes `useCan()` to hide Enterprise features that are *present* in the
 * paid bundle but not *entitled* on a Team deployment. This is UX only — the API
 * still enforces `@requires` regardless, so a hand-crafted request to an
 * Enterprise endpoint on a Team deployment is rejected. In the OSS build there is
 * no `/api/capabilities` route (it lives in `ee/`), so the fetch fails and
 * capabilities resolve empty; the OSS build has no Enterprise components anyway.
 *
 * Free<->paid is decided by the physical strip (ADR #99); this governs only the
 * team<->enterprise boundary, which is a runtime entitlement.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '../../utils';
import { queryKeys } from '../../lib/queryClient';
import { useAuth } from '../useAuth';

export interface Capabilities {
  tier: string;
  capabilities: string[];
}

const EMPTY: Capabilities = { tier: 'free', capabilities: [] };

/**
 * Fetch this deployment's tier + capabilities. The value is fixed for the life of
 * a deployment, so it is cached for the session. The fetch is gated on auth
 * readiness (the route is behind auth); on any failure (OSS 404, unauthenticated)
 * it resolves to the empty set, so `can()` returns false and Enterprise UI stays
 * hidden rather than the query surfacing an error.
 */
export function useCapabilitiesQuery() {
  const { isAuthenticated, isAuthEnabled } = useAuth();
  return useQuery<Capabilities>({
    queryKey: queryKeys.capabilities,
    queryFn: async () => {
      const data = await api.get('/capabilities');
      if (data.error) return EMPTY;
      return {
        tier: data.tier ?? 'free',
        capabilities: Array.isArray(data.capabilities) ? data.capabilities : [],
      };
    },
    enabled: isAuthenticated || !isAuthEnabled,
    staleTime: Infinity, // tier is fixed per deployment
    gcTime: Infinity,
    retry: false,
  });
}

/**
 * Returns a `can(capability)` predicate for gating Enterprise UI. Team features
 * need no gate (they are present on any paid deployment); apply this only to
 * Enterprise slots, alongside the existing presence check:
 *
 *   const Slot = paidSurface.CostReport;
 *   const can = useCan();
 *   {Slot && can('cost.reporting') ? <Slot /> : <EeFeatureFallback />}
 */
export function useCan(): (capability: string) => boolean {
  const { data } = useCapabilitiesQuery();
  const caps = data?.capabilities ?? EMPTY.capabilities;
  return (capability: string) => caps.includes(capability);
}

/** This deployment's tier: 'free' | 'team' | 'enterprise'. */
export function useTier(): string {
  const { data } = useCapabilitiesQuery();
  return data?.tier ?? EMPTY.tier;
}
