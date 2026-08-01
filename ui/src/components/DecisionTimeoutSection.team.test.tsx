import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Team build: paid surface has a section, so editing is enabled.
vi.mock('@/ee-active.generated', () => ({
  paidSurface: { AlertsSection: () => null },
}));
const putMock = vi.fn(async () => ({ ok: true, data: { decision_timeout_seconds: 3600 } }));
vi.mock('@/utils/api', () => ({
  api: {
    get: vi.fn(async () => ({ ok: true, data: { decision_timeout_seconds: 7200 } })),
    put: (...args: unknown[]) => putMock(...args),
  },
  isOk: (r: { ok?: boolean }) => !!r?.ok,
}));

import { DecisionTimeoutSection } from './DecisionTimeoutSection';

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>
  );
}

describe('DecisionTimeoutSection (Team)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('is editable with a Save button on Team', async () => {
    renderWithQuery(<DecisionTimeoutSection />);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument();
    });
    const input = screen.getByLabelText(/Wait for/) as HTMLInputElement;
    expect(input.disabled).toBe(false);
  });

  it('saves the value in seconds (hours * 3600)', async () => {
    renderWithQuery(<DecisionTimeoutSection />);
    const input = await screen.findByLabelText(/Wait for/);
    fireEvent.change(input, { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: /save/i }));
    await waitFor(() => {
      expect(putMock).toHaveBeenCalledWith('/settings/decision-timeout', {
        decision_timeout_seconds: 10800,
      });
    });
  });
});
