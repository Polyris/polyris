import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import { PipelinesSidebar } from './PipelinesSidebar';
import { createPipeline, createPipelines } from '../test/factories';
import { useAppStore } from '../stores/useAppStore';
import type { PipelineWithUI } from '../types';
import config from '../lib/config';

vi.mock('../lib/config', () => ({ default: { API_URL: '/api', POLLING_INTERVAL: 5000, AUTH_ENABLED: false } }));
vi.mock('./Skeletons', () => ({ PipelineListSkeleton: () => <div data-testid="skeleton" /> }));

const refetch = vi.fn();
const mockQuery = { data: [] as PipelineWithUI[], isLoading: false, isError: false, error: null as Error | null, refetch };
const mockStats = { incomplete: false };
vi.mock('@/hooks/queries', () => ({
    usePipelinesQuery: () => mockQuery,
    usePipelinesStatsIncomplete: () => mockStats.incomplete,
}));

const pipelines = (...list: Array<Partial<PipelineWithUI> & { name: string }>) =>
    list.map(item => createPipeline(item) as PipelineWithUI);

const list = () => document.querySelector('.sb-list') as HTMLElement;
const rowNames = () => [...list().querySelectorAll('.sb-row .sb-row__name')].map(el => el.textContent);
const groupButton = (name: RegExp) => screen.getByRole('button', { name });

describe('PipelinesSidebar', () => {
    beforeEach(() => {
        localStorage.clear();
        refetch.mockReset();
        mockQuery.data = createPipelines(5) as PipelineWithUI[];
        mockQuery.isLoading = false;
        mockQuery.isError = false;
        mockQuery.error = null;
        mockStats.incomplete = false;
        const store = useAppStore.getState();
        store.setSelectedPipeline(null);
        store.setPipelineSearch('');
        store.setSidebarOpen(false);
    });

    describe('states', () => {
        it('shows the title and the pipeline count', () => {
            render(<PipelinesSidebar />);
            expect(screen.getByText('Pipelines')).toBeInTheDocument();
            expect(screen.getByText('5 pipelines')).toBeInTheDocument();
        });

        it('shows the skeleton while loading', () => {
            mockQuery.isLoading = true;
            mockQuery.data = [];
            render(<PipelinesSidebar />);
            expect(screen.getByTestId('skeleton')).toBeInTheDocument();
        });

        it('says when no pipeline is registered', () => {
            mockQuery.data = [];
            render(<PipelinesSidebar />);
            expect(screen.getByText('No pipelines')).toBeInTheDocument();
        });

        it('a failed load says what failed and offers a retry, instead of an empty list', () => {
            mockQuery.data = [];
            mockQuery.isError = true;
            mockQuery.error = new Error('HTTP 503');
            render(<PipelinesSidebar />);
            expect(screen.getByRole('alert')).toHaveTextContent("Couldn't load pipelines");
            expect(screen.getByRole('alert')).toHaveTextContent('server error');
            expect(screen.queryByText('No pipelines')).not.toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
            expect(refetch).toHaveBeenCalledTimes(1);
        });
    });

    describe('rows', () => {
        it('lists pipelines A–Z', () => {
            mockQuery.data = pipelines({ name: 'zebra' }, { name: 'alpha' }, { name: 'middle' });
            render(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['alpha', 'middle', 'zebra']);
        });

        it.each([
            ['cron(0 8 * * ? *)', 'daily @ 08:00'],
            ['cron(0 10 ? * MON *)', 'Mon @ 10:00'],
            ['rate(6 hours)', 'every 6h'],
            ['', 'Manual'],
        ])('shows schedule %j as %j', (schedule, shown) => {
            mockQuery.data = pipelines({ name: 'one', schedule });
            const { container } = render(<PipelinesSidebar />);
            expect(container.querySelector('.sb-row__schedule')).toHaveTextContent(shown);
        });

        it('the sparkline has ten slots, newest run rightmost', () => {
            mockQuery.data = pipelines({
                name: 'one',
                recent_runs: [
                    { date: '2026-02-19', status: 'running' },
                    { date: '2026-02-18', status: 'failed' },
                    { date: '2026-02-17', status: 'success' },
                ],
            });
            const { container } = render(<PipelinesSidebar />);
            const bars = [...container.querySelectorAll('.sb-spark__bar')];
            expect(bars).toHaveLength(10);
            expect(bars.slice(0, 7).every(b => b.className === 'sb-spark__bar')).toBe(true);
            expect(bars.slice(7).map(b => b.className)).toEqual([
                'sb-spark__bar sb-spark__bar--success',
                'sb-spark__bar sb-spark__bar--failed',
                'sb-spark__bar sb-spark__bar--running',
            ]);
        });

        it('a pipeline with no recent runs still has ten empty slots', () => {
            mockQuery.data = pipelines({ name: 'one', recent_runs: null });
            const { container } = render(<PipelinesSidebar />);
            expect(container.querySelectorAll('.sb-spark__bar')).toHaveLength(10);
            expect(container.querySelector('[class*="sb-spark__bar--"]')).toBeNull();
        });
    });

    describe('needs attention', () => {
        it('pins failed pipelines on top; running and aborted are not pinned', () => {
            mockQuery.data = pipelines(
                { name: 'a-ok', status: 'success' },
                { name: 'b-stopped', status: 'aborted' },
                { name: 'c-run', status: 'running' },
                { name: 'd-fail', status: 'failed' },
            );
            render(<PipelinesSidebar />);
            expect(screen.getByRole('heading', { name: /Needs attention/ })).toHaveTextContent('1');
            expect(rowNames()).toEqual(['d-fail', 'a-ok', 'b-stopped', 'c-run', 'd-fail']);
        });

        it('is hidden while searching', () => {
            mockQuery.data = pipelines({ name: 'a-fail', status: 'failed' }, { name: 'b' }, { name: 'c' }, { name: 'd' });
            useAppStore.getState().setPipelineSearch('a-fail');
            render(<PipelinesSidebar />);
            expect(screen.queryByRole('heading', { name: /Needs attention/ })).not.toBeInTheDocument();
            expect(rowNames()).toEqual(['a-fail']);
        });
    });

    describe('groups', () => {
        it('groups only by group=; names alone make no group', () => {
            mockQuery.data = pipelines({ name: 'acme-daily', group: '' }, { name: 'acme-weekly', group: '' });
            render(<PipelinesSidebar />);
            expect(screen.queryByRole('button', { name: /group,/ })).not.toBeInTheDocument();
            expect(rowNames()).toEqual(['acme-daily', 'acme-weekly']);
        });

        it('lists groups A–Z with Ungrouped last', () => {
            mockQuery.data = pipelines(
                { name: 'solo', group: '' },
                { name: 'orders-clean', group: 'orders' },
                { name: 'inv', group: 'analytics' },
            );
            render(<PipelinesSidebar />);
            const groups = screen.getAllByRole('button', { name: /group,/ }).map(b => b.getAttribute('aria-label'));
            expect(groups).toEqual([
                'analytics group, 1 pipeline',
                'orders group, 1 pipeline',
                'Ungrouped group, 1 pipeline',
            ]);
        });

        it('folding a group hides its rows, keeps a failure visible, and is remembered', () => {
            mockQuery.data = pipelines(
                { name: 'orders-clean', group: 'orders', status: 'failed' },
                { name: 'orders-report', group: 'orders' },
                { name: 'sales', group: 'sales' },
            );
            const { unmount } = render(<PipelinesSidebar />);
            fireEvent.click(groupButton(/^orders group/));

            const folded = groupButton(/^orders group/);
            expect(folded).toHaveAttribute('aria-expanded', 'false');
            expect(within(folded).getByText('1 failed')).toBeInTheDocument();
            // orders-clean stays under "Needs attention"; orders-report is folded away.
            expect(rowNames()).toEqual(['orders-clean', 'sales']);

            unmount();
            render(<PipelinesSidebar />);
            expect(groupButton(/^orders group/)).toHaveAttribute('aria-expanded', 'false');
        });

        it('marks a folded group that contains the open pipeline', () => {
            localStorage.setItem('sidebarFoldedGroups', JSON.stringify(['orders']));
            mockQuery.data = pipelines({ name: 'orders-clean', group: 'orders' }, { name: 'sales', group: 'sales' });
            useAppStore.getState().setSelectedPipeline(mockQuery.data[0]);
            render(<PipelinesSidebar />);
            expect(groupButton(/^orders group/)).toHaveAccessibleName(/contains the open pipeline/);
            expect(groupButton(/^sales group/)).not.toHaveAccessibleName(/contains the open pipeline/);
        });

        it('starts with every group open when the remembered value is unreadable', () => {
            localStorage.setItem('sidebarFoldedGroups', 'not json');
            mockQuery.data = pipelines({ name: 'a', group: 'g1' }, { name: 'b', group: 'g2' });
            render(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['a', 'b']);
        });

        it('a search shows matches from folded groups too', () => {
            localStorage.setItem('sidebarFoldedGroups', JSON.stringify(['orders']));
            mockQuery.data = pipelines(
                { name: 'orders-clean', group: 'orders' },
                { name: 'orders-report', group: 'orders' },
                { name: 'sales', group: 'sales' },
                { name: 'misc', group: 'sales' },
            );
            useAppStore.getState().setPipelineSearch('orders');
            render(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['orders-clean', 'orders-report']);
        });
    });

    describe('search', () => {
        it('is shown for more than three pipelines only', () => {
            mockQuery.data = pipelines({ name: 'a' }, { name: 'b' }, { name: 'c' });
            render(<PipelinesSidebar />);
            expect(screen.queryByPlaceholderText(/Search pipelines/i)).not.toBeInTheDocument();
        });

        it('typing filters with every word (AND) and updates the count', () => {
            mockQuery.data = pipelines({ name: 'acme-daily' }, { name: 'acme-weekly' }, { name: 'shop-daily' }, { name: 'x' });
            render(<PipelinesSidebar />);
            fireEvent.change(screen.getByPlaceholderText(/Search pipelines/i), { target: { value: 'acme daily' } });
            expect(useAppStore.getState().pipelineSearch).toBe('acme daily');
            expect(rowNames()).toEqual(['acme-daily']);
            expect(screen.getByText('1 pipeline')).toBeInTheDocument();
        });

        it('says when nothing matches', () => {
            useAppStore.getState().setPipelineSearch('nonexistent');
            render(<PipelinesSidebar />);
            expect(screen.getByText('No matches')).toBeInTheDocument();
        });

        it('Esc and the clear button empty the search', () => {
            render(<PipelinesSidebar />);
            const input = screen.getByPlaceholderText(/Search pipelines/i);
            fireEvent.change(input, { target: { value: 'acme' } });
            fireEvent.keyDown(input, { key: 'Escape' });
            expect(useAppStore.getState().pipelineSearch).toBe('');

            fireEvent.change(input, { target: { value: 'acme' } });
            fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
            expect(useAppStore.getState().pipelineSearch).toBe('');
            expect(input).toHaveFocus();
        });
    });

    describe('keyboard', () => {
        beforeEach(() => {
            mockQuery.data = pipelines({ name: 'a' }, { name: 'b' }, { name: 'c' }, { name: 'd' });
        });

        it('"/" focuses the search, and ↓ moves from it into the list', () => {
            render(<PipelinesSidebar />);
            fireEvent.keyDown(document.body, { key: '/' });
            const input = screen.getByPlaceholderText(/Search pipelines/i);
            expect(input).toHaveFocus();
            fireEvent.keyDown(input, { key: 'ArrowDown' });
            expect(document.activeElement?.querySelector('.sb-row__name')?.textContent).toBe('a');
        });

        it.each([
            ['a modal', { 'aria-modal': 'true' }],
            ['the command palette or a popover', {}],
        ])('"/" leaves the focus in %s', (_, extra) => {
            render(<><PipelinesSidebar /><div role="dialog" {...extra}><button>inside</button></div></>);
            screen.getByText('inside').focus();
            fireEvent.keyDown(document.body, { key: '/' });
            expect(screen.getByText('inside')).toHaveFocus();
        });

        it('the list is one Tab stop; j / k and the arrows move through it', () => {
            render(<PipelinesSidebar />);
            const items = [...list().querySelectorAll('button')];
            expect(items.filter(b => b.tabIndex === 0)).toHaveLength(1);

            items[0].focus();
            fireEvent.keyDown(items[0], { key: 'j' });
            expect(items[1]).toHaveFocus();
            fireEvent.keyDown(items[1], { key: 'ArrowDown' });
            expect(items[2]).toHaveFocus();
            fireEvent.keyDown(items[2], { key: 'k' });
            expect(items[1]).toHaveFocus();
            expect(items.filter(b => b.tabIndex === 0)).toEqual([items[1]]);
        });

        it('the Tab stop starts on the open pipeline', () => {
            useAppStore.getState().setSelectedPipeline(mockQuery.data[2]);
            render(<PipelinesSidebar />);
            const stop = [...list().querySelectorAll('button')].find(b => b.tabIndex === 0);
            expect(stop?.querySelector('.sb-row__name')?.textContent).toBe('c');
        });
    });

    describe('refetch while the list is in use', () => {
        const before = () => pipelines({ name: 'a-fail', status: 'failed' }, { name: 'b' }, { name: 'c' }, { name: 'd' });
        const after = () => pipelines({ name: 'a-fail', status: 'success' }, { name: 'b' }, { name: 'c' }, { name: 'd' }, { name: 'e' });

        it('keeps the rows still under the pointer, and shows the new data when it leaves', () => {
            mockQuery.data = before();
            const { rerender } = render(<PipelinesSidebar />);
            fireEvent.mouseEnter(list());
            mockQuery.data = after();
            rerender(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['a-fail', 'a-fail', 'b', 'c', 'd']);

            fireEvent.mouseLeave(list());
            expect(rowNames()).toEqual(['a-fail', 'b', 'c', 'd', 'e']);
        });

        it('keeps the focused row in place, so the keyboard does not lose it', () => {
            mockQuery.data = before();
            const { rerender } = render(<PipelinesSidebar />);
            const pinned = list().querySelector<HTMLButtonElement>('.sb-row')!;
            act(() => pinned.focus());
            mockQuery.data = after();
            rerender(<PipelinesSidebar />);
            expect(pinned).toHaveFocus();
            expect(rowNames()[0]).toBe('a-fail');

            act(() => screen.getByPlaceholderText(/Search pipelines/i).focus());
            expect(rowNames()).toEqual(['a-fail', 'b', 'c', 'd', 'e']);
        });

        it('a held row still shows the fresh status', () => {
            mockQuery.data = before();
            const { rerender } = render(<PipelinesSidebar />);
            fireEvent.mouseEnter(list());
            mockQuery.data = after();
            rerender(<PipelinesSidebar />);
            const pinned = list().querySelector('.sb-row')!;
            expect(pinned).toHaveAccessibleName(/^a-fail, last run: success/);
        });

        it("a folded group's failure count follows the data while the rows are held", () => {
            localStorage.setItem('sidebarFoldedGroups', JSON.stringify(['orders']));
            mockQuery.data = pipelines({ name: 'o1', group: 'orders' }, { name: 'o2', group: 'orders' }, { name: 's', group: 'sales' }, { name: 't', group: 'sales' });
            const { rerender } = render(<PipelinesSidebar />);
            fireEvent.mouseEnter(list());
            mockQuery.data = pipelines({ name: 'o1', group: 'orders', status: 'failed' }, { name: 'o2', group: 'orders' }, { name: 's', group: 'sales' }, { name: 't', group: 'sales' });
            rerender(<PipelinesSidebar />);
            expect(groupButton(/^orders group/)).toHaveAccessibleName(/1 failed/);
        });

        it('when a focused pinned row leaves, the focus moves to the same pipeline in its group', () => {
            mockQuery.data = before();
            const { rerender } = render(<PipelinesSidebar />);
            const pinned = list().querySelector<HTMLButtonElement>('.sb-row')!;
            act(() => pinned.focus());
            fireEvent.click(pinned);
            mockQuery.data = after();
            rerender(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['a-fail', 'b', 'c', 'd', 'e']);
            expect(document.activeElement?.querySelector('.sb-row__name')?.textContent).toBe('a-fail');
        });

        it('opening a pipeline lets the rows follow the data, though the focus stays on its row', () => {
            mockQuery.data = before();
            const { rerender } = render(<PipelinesSidebar />);
            const row = screen.getAllByRole('button', { name: /^b,/ })[0];
            act(() => row.focus());
            fireEvent.click(row);
            mockQuery.data = after();
            rerender(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['a-fail', 'b', 'c', 'd', 'e']);
        });

        it('a pointer resting on the list during the first load still shows the list', () => {
            mockQuery.isLoading = true;
            mockQuery.data = [];
            const { rerender } = render(<PipelinesSidebar />);
            fireEvent.mouseEnter(list());
            mockQuery.isLoading = false;
            mockQuery.data = before();
            rerender(<PipelinesSidebar />);
            expect(rowNames()).toContain('b');
        });
    });

    describe('selection', () => {
        it('clicking a row opens the pipeline, leaves "current structure" mode and closes the drawer', () => {
            useAppStore.getState().setDagViewSource('current');
            useAppStore.getState().setSidebarOpen(true);
            render(<PipelinesSidebar />);
            fireEvent.click(screen.getByRole('button', { name: /^acme-daily,/ }));
            const state = useAppStore.getState();
            expect(state.selectedPipeline).toEqual(expect.objectContaining({ name: 'acme-daily' }));
            expect(state.dagViewSource).toBe('run');
            expect(state.sidebarOpen).toBe(false);
        });

        it('highlights every row of the open pipeline and announces only the first as current', () => {
            mockQuery.data = pipelines({ name: 'a-fail', status: 'failed' }, { name: 'b' });
            useAppStore.getState().setSelectedPipeline(mockQuery.data[0]);
            render(<PipelinesSidebar />);
            const names = (sel: string) => [...list().querySelectorAll(sel)].map(el => el.querySelector('.sb-row__name')?.textContent);
            expect(names('.sb-row--selected')).toEqual(['a-fail', 'a-fail']);
            expect(names('[aria-current="page"]')).toEqual(['a-fail']);
        });
    });

    describe('review follow-ups', () => {
        it('Enter in the search opens the first match', () => {
            mockQuery.data = pipelines({ name: 'acme-a' }, { name: 'acme-b' }, { name: 'shop' }, { name: 'x' });
            render(<PipelinesSidebar />);
            const input = screen.getByPlaceholderText(/Search pipelines/i);
            fireEvent.change(input, { target: { value: 'acme' } });
            fireEvent.keyDown(input, { key: 'Enter' });
            expect(useAppStore.getState().selectedPipeline?.name).toBe('acme-a');
        });

        it('Esc in the list goes back to the search', () => {
            mockQuery.data = pipelines({ name: 'a' }, { name: 'b' }, { name: 'c' }, { name: 'd' });
            render(<PipelinesSidebar />);
            const row = list().querySelector<HTMLButtonElement>('.sb-row')!;
            act(() => row.focus());
            fireEvent.keyDown(row, { key: 'Escape' });
            expect(screen.getByPlaceholderText(/Search pipelines/i)).toHaveFocus();
        });

        it('a search finds a pipeline by its group, and shows the group', () => {
            mockQuery.data = pipelines(
                { name: 'clean', group: 'orders' }, { name: 'report', group: 'orders' },
                { name: 'stock', group: 'inventory' }, { name: 'misc', group: 'inventory' },
            );
            useAppStore.getState().setPipelineSearch('orders');
            render(<PipelinesSidebar />);
            expect(rowNames()).toEqual(['clean', 'report']);
            expect(list().querySelector('.sb-group--static')).toHaveTextContent('orders');
        });

        it('"Needs attention" folds, keeps its count, and is remembered', () => {
            mockQuery.data = pipelines({ name: 'a-fail', status: 'failed' }, { name: 'b' });
            const { unmount } = render(<PipelinesSidebar />);
            fireEvent.click(screen.getByRole('button', { name: /Needs attention/ }));
            expect(screen.getByRole('button', { name: /Needs attention/ })).toHaveAttribute('aria-expanded', 'false');
            expect(rowNames()).toEqual(['a-fail', 'b']);
            unmount();
            render(<PipelinesSidebar />);
            expect(screen.getByRole('button', { name: /Needs attention/ })).toHaveAttribute('aria-expanded', 'false');
        });

        it('folds and unfolds every group at once', () => {
            mockQuery.data = pipelines({ name: 'a', group: 'g1' }, { name: 'b', group: 'g2' });
            render(<PipelinesSidebar />);
            fireEvent.click(screen.getByRole('button', { name: 'Fold all groups' }));
            expect(rowNames()).toEqual([]);
            fireEvent.click(screen.getByRole('button', { name: 'Unfold all groups' }));
            expect(rowNames()).toEqual(['a', 'b']);
        });

        it('pins a run that waits for a decision and says so', () => {
            mockQuery.data = pipelines({ name: 'approve', status: 'running', waiting_on: 'waiting_decision' }, { name: 'b' });
            render(<PipelinesSidebar />);
            expect(screen.getByRole('button', { name: /Needs attention/ })).toHaveTextContent('1');
            expect(screen.getAllByRole('button', { name: /^approve, last run: running, waiting for a decision/ })).toHaveLength(2);
        });

        it("a row's accessible name sums up its runs and gives the whole schedule", () => {
            mockQuery.data = pipelines({
                name: 'one',
                schedule: '',
                asset_schedule: { operator: 'AND', assets: ['raw_orders_from_the_warehouse', 'currency_rates'] },
                recent_runs: [{ status: 'failed' }, { status: 'success' }, { status: 'success' }],
            });
            render(<PipelinesSidebar />);
            const row = list().querySelector('.sb-row')!;
            expect(row).toHaveAccessibleName(/last 3 runs: 1 failed, 2 success/);
            expect(row).toHaveAccessibleName(/schedule: raw_orders_from_the_warehouse & currency_rates$/);
        });

        it('says when some runs could not be read', () => {
            mockStats.incomplete = true;
            render(<PipelinesSidebar />);
            expect(screen.getByRole('status')).toHaveTextContent('Some recent runs could not be read');
        });

        it('offers no retry while the console has no API URL', () => {
            const saved = config.API_URL;
            (config as { API_URL: string }).API_URL = '';
            try {
                mockQuery.data = [];
                mockQuery.isError = true;
                render(<PipelinesSidebar />);
                expect(screen.getByRole('alert')).toHaveTextContent('Configure API_URL');
                expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
            } finally {
                (config as { API_URL: string }).API_URL = saved;
            }
        });
    });

    describe('mobile drawer', () => {
        it('opens with an overlay that closes it', () => {
            useAppStore.getState().setSidebarOpen(true);
            const { container } = render(<PipelinesSidebar />);
            expect(container.querySelector('.sidebar-open')).toBeInTheDocument();
            fireEvent.click(container.querySelector('.sidebar-overlay')!);
            expect(useAppStore.getState().sidebarOpen).toBe(false);
        });
    });
});
