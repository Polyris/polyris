/**
 * useCapabilities / useCan / useTier tests (ADR #100).
 *
 * The deployment-entitlement hooks that gate Enterprise UI on a Team deployment.
 * Mocks the `api` and `useAuth` boundaries (per CLAUDE.md #14) so the real React
 * Query + hook logic runs under test. Covers: success, graceful empty on the OSS
 * 404 / unauthenticated case (no throw), auth-readiness gating, and the `can()` /
 * tier derivations.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// ── Boundary mocks ───────────────────────────────────────────────────────────
const { mockApi, authState } = vi.hoisted(() => ({
  mockApi: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  authState: { isAuthenticated: true, isAuthEnabled: true },
}));
vi.mock('../../utils', () => ({ api: mockApi }));
vi.mock('../useAuth', () => ({ useAuth: () => authState }));

import { useCapabilitiesQuery, useCan, useTier } from './useCapabilities';

function wrapper(qc: QueryClient) {
  const W = ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: qc }, children);
  W.displayName = 'TestQCWrapper';
  return W;
}
const makeQc = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

beforeEach(() => {
  vi.clearAllMocks();
  authState.isAuthenticated = true;
  authState.isAuthEnabled = true;
});

describe('useCapabilitiesQuery', () => {
  it('returns tier + capabilities on success', async () => {
    mockApi.get.mockResolvedValue({ ok: true, tier: 'enterprise', capabilities: ['cost.reporting', 'rbac'] });
    const { result } = renderHook(() => useCapabilitiesQuery(), { wrapper: wrapper(makeQc()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ tier: 'enterprise', capabilities: ['cost.reporting', 'rbac'] });
    expect(mockApi.get).toHaveBeenCalledWith('/capabilities');
  });

  it('resolves to empty (free) on error — OSS 404 / unauth, no throw', async () => {
    mockApi.get.mockResolvedValue({ ok: false, error: 'HTTP 404', status: 404 });
    const { result } = renderHook(() => useCapabilitiesQuery(), { wrapper: wrapper(makeQc()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ tier: 'free', capabilities: [] });
  });

  it('does not fetch until auth is ready', () => {
    authState.isAuthenticated = false; // auth enabled but not signed in
    const { result } = renderHook(() => useCapabilitiesQuery(), { wrapper: wrapper(makeQc()) });
    expect(mockApi.get).not.toHaveBeenCalled();
    expect(result.current.fetchStatus).toBe('idle');
  });

  it('fetches when auth is disabled (no login required)', async () => {
    authState.isAuthenticated = false;
    authState.isAuthEnabled = false; // no auth -> route reachable without a token
    mockApi.get.mockResolvedValue({ ok: true, tier: 'team', capabilities: ['backfill'] });
    const { result } = renderHook(() => useCapabilitiesQuery(), { wrapper: wrapper(makeQc()) });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockApi.get).toHaveBeenCalled();
  });
});

describe('useCan', () => {
  it('grants only capabilities in the set', async () => {
    mockApi.get.mockResolvedValue({ ok: true, tier: 'team', capabilities: ['backfill', 'pipeline.observability'] });
    const { result } = renderHook(() => useCan(), { wrapper: wrapper(makeQc()) });
    await waitFor(() => expect(result.current('backfill')).toBe(true));
    expect(result.current('pipeline.observability')).toBe(true);
    expect(result.current('cost.reporting')).toBe(false); // enterprise cap absent on team
  });

  it('denies everything before data loads / on empty', () => {
    authState.isAuthenticated = false; // disabled -> no data
    const { result } = renderHook(() => useCan(), { wrapper: wrapper(makeQc()) });
    expect(result.current('anything')).toBe(false);
  });
});

describe('useTier', () => {
  it('returns the deployment tier', async () => {
    mockApi.get.mockResolvedValue({ ok: true, tier: 'enterprise', capabilities: [] });
    const { result } = renderHook(() => useTier(), { wrapper: wrapper(makeQc()) });
    await waitFor(() => expect(result.current).toBe('enterprise'));
  });

  it("defaults to 'free' before load", () => {
    authState.isAuthenticated = false;
    const { result } = renderHook(() => useTier(), { wrapper: wrapper(makeQc()) });
    expect(result.current).toBe('free');
  });
});
