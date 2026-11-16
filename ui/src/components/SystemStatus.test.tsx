/**
 * SystemStatus — the System pill and its panel (contract §3).
 *
 * Only the boundaries are mocked: `api.get`, and where a test needs them the Clipboard API
 * and `matchMedia`. The query, the rule, the toasts, BaseModal and the shortcuts run for real.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock('@/utils/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/utils/api')>()),
    api: { get: mockGet },
}));

import { SystemStatus } from './SystemStatus';
import { ToastProvider } from './Toast';
import {
    apiAnswered,
    apiFailed,
    createFinding,
    createHealthBody,
    createMinimalHealthBody,
} from '../test/factories';

// The fixtures check at 11:58:00.123 UTC; the suite runs in UTC.
const NOW = new Date('2026-11-14T12:00:30Z');

const MAPPING_OFF = createFinding();
const SWEEPER_SILENT = createFinding({
    title: 'The periodic re-check is not running',
    resource: 'The last run of polyris-dev-polyris-sweeper was 22 min ago. It runs every 5 minutes.',
    changed_at: undefined,
    impact: 'Tasks still start from their signals. A task that missed its signal is not re-checked and waits until it times out.',
    fix: 'Check that its schedule in EventBridge Scheduler is enabled. If it is, read the function\'s logs.',
    command: 'aws scheduler get-schedule --name polyris-dev-polyris-sweeper --region us-east-1 --query State\naws logs tail /aws/lambda/polyris-dev-polyris-sweeper --region us-east-1 --since 1h',
});

const HEALTHY = apiAnswered(createHealthBody());
const ATTENTION = apiAnswered(createHealthBody({ coordination: 'unhealthy' }, [MAPPING_OFF]));
const toastText = (title: string) => `System needs attention: ${title}. Open System in the header for details.`;

const LOST = 'Could not reach the API. Check your connection and that the console API URL is set correctly.';

function renderStatus({ apiConnected = true } = {}) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const tree = (connected: boolean) => (
        <QueryClientProvider client={client}>
            <ToastProvider>
                <button type="button">Elsewhere</button>
                <SystemStatus connectionError={connected ? null : LOST} />
            </ToastProvider>
        </QueryClientProvider>
    );
    const view = render(tree(apiConnected));
    return { ...view, client, setConnected: (connected: boolean) => view.rerender(tree(connected)) };
}

/** jsdom has no Clipboard API: give navigator one (or explicitly none) for a test. */
function stubClipboard(clipboard: unknown) {
    Object.defineProperty(navigator, 'clipboard', { value: clipboard, configurable: true });
}

const pill = () => screen.getByRole('button', { name: /^System/ });
const panel = () => screen.getByRole('dialog', { name: 'System status' });
const liveRegion = () => document.querySelector('.sys-status > .sr-only') as HTMLElement;

async function pillShows(label: string) {
    await waitFor(() => expect(pill()).toHaveAccessibleName(label));
}

async function openPanel() {
    fireEvent.click(pill());
    return panel();
}

beforeEach(() => {
    mockGet.mockReset();
    sessionStorage.clear();
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
});

afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    delete (navigator as { clipboard?: unknown }).clipboard;
});

// ─── The pill ────────────────────────────────────────────────────────────────

describe('the pill', () => {
    it('is neutral "System" until the first answer', () => {
        mockGet.mockReturnValue(new Promise(() => {}));
        renderStatus();
        expect(pill()).toHaveAccessibleName('System');
        expect(pill()).toHaveTextContent(/^System$/);
        expect(pill()).toHaveAttribute('data-level', 'loading');
        expect(pill().querySelector('svg.lucide-circle')).not.toBeNull();
    });

    it('is a quiet green "System" when healthy; its name adds "all checks pass"', async () => {
        mockGet.mockResolvedValue(HEALTHY);
        renderStatus();
        await pillShows('System, all checks pass');
        expect(pill()).toHaveTextContent(/^System$/);
        expect(pill()).toHaveAttribute('data-level', 'healthy');
        expect(pill().querySelector('svg.lucide-circle-check')).not.toBeNull();
        expect(mockGet).toHaveBeenCalledWith('/health');
    });

    it('stays healthy when DynamoDB or Step Functions is only degraded', async () => {
        mockGet.mockResolvedValue(apiAnswered(createHealthBody({ dynamodb: 'degraded', stepfunctions: 'degraded' })));
        renderStatus();
        await pillShows('System, all checks pass');
    });

    it('needs attention when a check is unhealthy', async () => {
        mockGet.mockResolvedValue(ATTENTION);
        renderStatus();
        await pillShows('System · needs attention');
        expect(pill()).toHaveTextContent('System · needs attention');
        expect(pill()).toHaveAttribute('data-level', 'attention');
        expect(pill().querySelector('svg.lucide-triangle-alert')).not.toBeNull();
    });

    it('reads a 503 that carries a body as attention, with its findings', async () => {
        const reach = createFinding({
            title: 'The console cannot reach DynamoDB',
            resource: 'AccessDeniedException: not authorized to perform dynamodb:GetItem',
            changed_at: undefined,
            impact: 'The console cannot read or change pipelines and tasks.',
            fix: 'Check the console API\'s role and the AWS Health Dashboard, then read the console API\'s logs.',
            command: undefined,
        });
        const body = { ...createHealthBody({ dynamodb: 'unhealthy' }, [reach]), status: 'unhealthy' };
        mockGet.mockResolvedValue(apiFailed('HTTP 503', 503, body));
        renderStatus();
        await pillShows('System · needs attention');
        const dialog = await openPanel();
        expect(within(dialog).getByText('The console cannot reach DynamoDB')).toBeInTheDocument();
        expect(within(dialog).getByText('AccessDeniedException: not authorized to perform dynamodb:GetItem')).toBeInTheDocument();
    });

    it('is grey "status unknown" when the request fails without a body', async () => {
        mockGet.mockResolvedValue(apiFailed('Failed to fetch'));
        renderStatus();
        await pillShows('System · status unknown');
        expect(pill()).toHaveAttribute('data-level', 'unknown');
        expect(pill().querySelector('svg.lucide-circle-dashed')).not.toBeNull();
    });

    it('turns unknown when a read fails after an earlier success', async () => {
        mockGet.mockResolvedValueOnce(HEALTHY).mockResolvedValueOnce(apiFailed('HTTP 502', 502));
        renderStatus();
        await pillShows('System, all checks pass');
        await openPanel();       // opening re-reads the status
        await pillShows('System · status unknown');
        expect(mockGet).toHaveBeenCalledTimes(2);
    });

    it('is unknown while the console cannot reach its API, whatever /api/health said', async () => {
        mockGet.mockResolvedValue(HEALTHY);
        const { setConnected } = renderStatus({ apiConnected: false });
        expect(pill()).toHaveAccessibleName('System · status unknown');
        await waitFor(() => expect(mockGet).toHaveBeenCalled());
        expect(pill()).toHaveAccessibleName('System · status unknown');
        setConnected(true);
        await pillShows('System, all checks pass');
        setConnected(false);
        expect(pill()).toHaveAccessibleName('System · status unknown');
    });

    it('is unknown, never green, when a word the rule needs is missing', async () => {
        const body = createHealthBody();
        const { coordination: _dropped, ...rest } = body.checks;
        mockGet.mockResolvedValue(apiAnswered({ ...body, checks: rest }));
        renderStatus();
        await pillShows('System · status unknown');
    });

    it('is unknown, never green, when a word is not recognised', async () => {
        mockGet.mockResolvedValue(apiAnswered(createHealthBody({ coordination: 'pending' })));
        renderStatus();
        await pillShows('System · status unknown');
    });
});

// ─── The panel ───────────────────────────────────────────────────────────────

describe('the panel', () => {
    it('opens on click as a popover, re-reading the status', async () => {
        mockGet.mockResolvedValue(HEALTHY);
        renderStatus();
        await pillShows('System, all checks pass');
        expect(pill()).toHaveAttribute('aria-expanded', 'false');
        const dialog = await openPanel();
        expect(pill()).toHaveAttribute('aria-expanded', 'true');
        expect(pill()).toHaveAttribute('aria-controls', dialog.id);
        expect(dialog).not.toHaveAttribute('aria-modal');
        await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    });

    it('lists the four passing checks when healthy', async () => {
        mockGet.mockResolvedValue(HEALTHY);
        renderStatus();
        await pillShows('System, all checks pass');
        const dialog = await openPanel();
        expect(within(dialog).getByText('All checks pass · checked 2 min ago')).toBeInTheDocument();
        for (const row of [
            'The console API reaches DynamoDB and Step Functions',
            'Asset event delivery is on',
            'The periodic re-check is running (every 5 minutes)',
            'Waiting tasks are re-checked on time',
        ]) {
            expect(within(dialog).getByText(row)).toBeInTheDocument();
        }
    });

    it('shows one card per finding, then the footer, in the full view', async () => {
        mockGet.mockResolvedValue(apiAnswered(createHealthBody({ coordination: 'unhealthy' }, [SWEEPER_SILENT, MAPPING_OFF])));
        renderStatus();
        await pillShows('System · needs attention');
        const dialog = await openPanel();

        expect(within(dialog).getByText('2 problems · checked 2 min ago')).toBeInTheDocument();
        const cards = within(dialog).getAllByRole('listitem');
        expect(cards).toHaveLength(2);

        const card = within(cards[1]);
        expect(card.getByText(MAPPING_OFF.title).tagName).toBe('STRONG');
        expect(card.getByText(MAPPING_OFF.resource)).toBeInTheDocument();
        expect(card.getByText('Last changed Today at 11:20:00 (40 min ago)')).toBeInTheDocument();
        expect(card.getByText('Impact').tagName).toBe('DT');
        expect(card.getByText(MAPPING_OFF.impact)).toBeInTheDocument();
        expect(card.getByText('Fix').tagName).toBe('DT');
        expect(card.getByText(MAPPING_OFF.fix)).toBeInTheDocument();
        expect(card.getByText(MAPPING_OFF.command).closest('pre')).not.toBeNull();

        // No change time, no "Last changed" line; a two-line command stays two lines.
        expect(within(cards[0]).queryByText(/^Last changed/)).toBeNull();
        expect(within(cards[0]).getByText(/aws scheduler get-schedule/).textContent).toBe(SWEEPER_SILENT.command);

        expect(within(dialog).getByText('After a fix, the next check clears it within 5 minutes; reopen this panel to see it.')).toBeInTheDocument();
    });

    it('copies a command, says "Copied!" in its reserved status, then clears it', async () => {
        const writeText = vi.fn().mockResolvedValue(undefined);
        stubClipboard({ writeText });
        mockGet.mockResolvedValue(ATTENTION);
        renderStatus();
        await pillShows('System · needs attention');
        const dialog = await openPanel();

        const copy = within(dialog).getByRole('button', { name: 'Copy command: Asset event delivery is off' });
        const status = within(copy.parentElement as HTMLElement).getByRole('status');
        expect(status).toHaveTextContent('');
        fireEvent.click(copy);

        expect(writeText).toHaveBeenCalledWith(MAPPING_OFF.command);
        await waitFor(() => expect(status).toHaveTextContent('Copied!'));
        await waitFor(() => expect(status).toHaveTextContent(''), { timeout: 2500 });
    });

    it('says so when the clipboard is not available, rather than claim a copy', async () => {
        stubClipboard(undefined);
        mockGet.mockResolvedValue(ATTENTION);
        renderStatus();
        await pillShows('System · needs attention');
        const dialog = await openPanel();
        const copy = within(dialog).getByRole('button', { name: 'Copy command: Asset event delivery is off' });
        fireEvent.click(copy);
        expect(within(copy.parentElement as HTMLElement).getByRole('status')).toHaveTextContent('Copy failed');
    });

    it('lists the unhealthy checks by label in the minimal view, without a check time', async () => {
        mockGet.mockResolvedValue(apiAnswered(createMinimalHealthBody({ dynamodb: 'unhealthy', coordination: 'unhealthy' })));
        renderStatus();
        await pillShows('System · needs attention');
        const dialog = await openPanel();
        expect(within(dialog).getByText('2 problems')).toBeInTheDocument();
        expect(within(dialog).getAllByRole('listitem').map(item => item.textContent)).toEqual(['DynamoDB', 'Task coordination']);
        expect(within(dialog).getByText('The details need a verified sign-in. Sign in again; if that does not help, the console API logs say why.')).toBeInTheDocument();
        expect(within(dialog).queryByRole('button', { name: /^Copy command/ })).toBeNull();
    });

    it('still names the unhealthy checks when a full answer lists no finding', async () => {
        mockGet.mockResolvedValue(apiAnswered(createHealthBody({ stepfunctions: 'unhealthy' })));
        renderStatus();
        await pillShows('System · needs attention');
        const dialog = await openPanel();
        expect(within(dialog).getByText('1 problem · checked 2 min ago')).toBeInTheDocument();
        expect(within(dialog).getByText('Step Functions')).toBeInTheDocument();
        expect(within(dialog).queryByText('The details need a verified sign-in. Sign in again; if that does not help, the console API logs say why.')).toBeNull();
    });

    it('gives the reason and a Retry now that reads again when unknown', async () => {
        mockGet.mockResolvedValue(apiFailed('HTTP 502', 502));
        renderStatus();
        await pillShows('System · status unknown');
        const dialog = await openPanel();
        expect(within(dialog).getByText('The status could not be read: HTTP 502. The console retries every 2 minutes.')).toBeInTheDocument();
        await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));

        mockGet.mockResolvedValue(HEALTHY);
        fireEvent.click(await within(dialog).findByRole('button', { name: 'Retry now' }));
        await pillShows('System, all checks pass');
        expect(mockGet).toHaveBeenCalledTimes(3);
    });

    it('names the lost connection as the reason, says why, and retries the pipelines read too', async () => {
        mockGet.mockResolvedValue(HEALTHY);
        const { client } = renderStatus({ apiConnected: false });
        const invalidate = vi.spyOn(client, 'invalidateQueries');
        const dialog = await openPanel();
        expect(within(dialog).getByText(/^The status could not be read: the console has lost its connection to the API\./)).toBeInTheDocument();
        expect(within(dialog).getByText(LOST)).toBeInTheDocument();
        fireEvent.click(within(dialog).getByRole('button', { name: /Retry now|Checking/ }));
        expect(invalidate).toHaveBeenCalledWith({ queryKey: ['pipelines'] });
    });
});

// ─── Closing ─────────────────────────────────────────────────────────────────

describe('closing the popover', () => {
    beforeEach(() => mockGet.mockResolvedValue(HEALTHY));

    it('moves focus into the panel on open, and back to the pill after Esc', async () => {
        renderStatus();
        await pillShows('System, all checks pass');
        const dialog = await openPanel();
        await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus());
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(pill()).toHaveFocus();
        expect(pill()).toHaveAttribute('aria-expanded', 'false');
    });

    it('returns focus to the pill after ×', async () => {
        renderStatus();
        await pillShows('System, all checks pass');
        const dialog = await openPanel();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(pill()).toHaveFocus();
    });

    it('closes on a mousedown outside, but not on one inside', async () => {
        renderStatus();
        await pillShows('System, all checks pass');
        const dialog = await openPanel();
        fireEvent.mouseDown(within(dialog).getByText('Asset event delivery is on'));
        expect(panel()).toBeInTheDocument();
        fireEvent.mouseDown(screen.getByRole('button', { name: 'Elsewhere' }));
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(pill()).not.toHaveFocus();
    });

    it('closes when focus moves outside it, and stays open when focus leaves the window', async () => {
        renderStatus();
        await pillShows('System, all checks pass');
        const dialog = await openPanel();
        const close = within(dialog).getByRole('button', { name: 'Close' });
        fireEvent.focusOut(close, { relatedTarget: null });          // to another window: a terminal, say
        expect(panel()).toBeInTheDocument();
        fireEvent.focusOut(close, { relatedTarget: pill() });         // to the pill: still inside
        expect(panel()).toBeInTheDocument();
        fireEvent.focusOut(close, { relatedTarget: screen.getByRole('button', { name: 'Elsewhere' }) });
        expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('leaves Esc alone while the panel is closed', async () => {
        renderStatus();
        await pillShows('System, all checks pass');
        const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
        elsewhere.focus();
        const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
        document.dispatchEvent(event);
        expect(elsewhere).toHaveFocus();
        expect(event.defaultPrevented).toBe(false);
    });

    it('closes when the pill is clicked again', async () => {
        renderStatus();
        await pillShows('System, all checks pass');
        await openPanel();
        fireEvent.click(pill());
        expect(screen.queryByRole('dialog')).toBeNull();
    });
});

describe('on a narrow screen (≤768px)', () => {
    beforeEach(() => {
        mockGet.mockResolvedValue(ATTENTION);
        vi.stubGlobal('matchMedia', (query: string) => ({
            matches: query === '(max-width: 768px)',
            addEventListener: () => {},
            removeEventListener: () => {},
        }));
    });

    it('opens a modal, which a tap inside does not close', async () => {
        renderStatus();
        await pillShows('System · needs attention');
        fireEvent.click(pill());
        const dialog = screen.getByRole('dialog', { name: 'System status' });
        expect(dialog).toHaveAttribute('aria-modal', 'true');
        expect(within(dialog).getByText('1 problem · checked 2 min ago')).toBeInTheDocument();
        fireEvent.mouseDown(within(dialog).getByText(MAPPING_OFF.title));
        expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('closes on Esc and returns focus to the pill', async () => {
        renderStatus();
        await pillShows('System · needs attention');
        fireEvent.click(pill());
        expect(screen.getByRole('dialog')).toBeInTheDocument();
        fireEvent.keyDown(document, { key: 'Escape' });
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(pill()).toHaveFocus();
    });

    it('closes on its overlay and on ×', async () => {
        renderStatus();
        await pillShows('System · needs attention');
        fireEvent.click(pill());
        fireEvent.click(screen.getByRole('dialog').parentElement as HTMLElement);
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(pill()).toHaveFocus();

        fireEvent.click(pill());
        fireEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(pill()).toHaveFocus();
    });
});

// ─── Toast and live region ───────────────────────────────────────────────────

describe('the toast', () => {
    it('is raised once per new finding title per browser session', async () => {
        const both = apiAnswered(createHealthBody({ coordination: 'unhealthy' }, [MAPPING_OFF, SWEEPER_SILENT]));
        mockGet.mockResolvedValueOnce(ATTENTION).mockResolvedValueOnce(ATTENTION).mockResolvedValue(both);
        const first = renderStatus();
        expect(await screen.findByText(toastText(MAPPING_OFF.title))).toBeInTheDocument();

        await openPanel();                                   // the same finding again
        await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
        fireEvent.click(pill());
        expect(screen.getAllByText(toastText(MAPPING_OFF.title))).toHaveLength(1);

        await openPanel();                                   // a new finding joins
        expect(await screen.findByText(toastText(SWEEPER_SILENT.title))).toBeInTheDocument();
        expect(screen.getAllByText(toastText(MAPPING_OFF.title))).toHaveLength(1);

        first.unmount();                                     // a reload in the same session
        renderStatus();
        await pillShows('System · needs attention');
        expect(screen.queryByText(/^System needs attention:/)).toBeNull();
    });

    it('is not raised in the minimal view', async () => {
        mockGet.mockResolvedValue(apiAnswered(createMinimalHealthBody({ coordination: 'unhealthy' })));
        renderStatus();
        await pillShows('System · needs attention');
        expect(screen.queryByText(/^System needs attention:/)).toBeNull();
    });

    it('still comes once per title when sessionStorage is unavailable', async () => {
        vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('SecurityError'); });
        vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('SecurityError'); });
        mockGet.mockResolvedValue(ATTENTION);
        renderStatus();
        expect(await screen.findByText(toastText(MAPPING_OFF.title))).toBeInTheDocument();
        await openPanel();
        await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
        expect(screen.getAllByText(toastText(MAPPING_OFF.title))).toHaveLength(1);
    });
});

describe('the live region', () => {
    it('stays quiet for the first answer and speaks on a change of state', async () => {
        mockGet.mockResolvedValueOnce(HEALTHY).mockResolvedValue(ATTENTION);
        renderStatus();
        await pillShows('System, all checks pass');
        expect(liveRegion()).toHaveTextContent('');
        expect(liveRegion()).toHaveAttribute('role', 'status');

        await openPanel();
        await pillShows('System · needs attention');
        expect(liveRegion()).toHaveTextContent('System · needs attention');
    });
});
