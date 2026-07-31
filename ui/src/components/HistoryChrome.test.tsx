import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

const { mockPush } = vi.hoisted(() => ({ mockPush: vi.fn() }));

vi.mock('@/hooks/useClientRoute', () => ({
    useClientRoute: () => ({ pathname: '/runs/', push: mockPush, replace: vi.fn() }),
}));
vi.mock('@/components/ui/button', () => ({
    Button: (props: Record<string, unknown>) => (
        <button onClick={props.onClick as () => void} disabled={props.disabled as boolean}>{props.children as React.ReactNode}</button>
    ),
}));
vi.mock('../utils/icons', () => ({
    Activity: () => <span data-testid="icon-activity" />,
    ListTodo: () => <span data-testid="icon-listtodo" />,
    RefreshCw: () => <span data-testid="icon-refresh" />,
    Search: () => <span data-testid="icon-search" />,
    ChevronLeft: () => <span data-testid="icon-left" />,
    ChevronRight: () => <span data-testid="icon-right" />,
}));

import { HistoryChrome } from './HistoryChrome';

const baseProps = {
    mode: 'runs' as const,
    search: '',
    onSearch: vi.fn(),
    page: 0,
    setPage: vi.fn(),
    total: 25,
    pageCount: 3,
    pageSize: 10,
    onRefresh: vi.fn(),
    children: <div data-testid="table" />,
};

describe('HistoryChrome', () => {
    beforeEach(() => {
        mockPush.mockClear();
        baseProps.onSearch = vi.fn();
        baseProps.setPage = vi.fn();
        baseProps.onRefresh = vi.fn();
    });

    it('renders the History title and both toggle tabs', () => {
        render(<HistoryChrome {...baseProps} />);
        expect(screen.getByText('History')).toBeInTheDocument();
        expect(screen.getByRole('tab', { name: /runs/i })).toBeInTheDocument();
        expect(screen.getByRole('tab', { name: /tasks/i })).toBeInTheDocument();
    });

    it('marks the active tab from mode and pushes the other route on click', () => {
        render(<HistoryChrome {...baseProps} mode="runs" />);
        const runsTab = screen.getByRole('tab', { name: /runs/i });
        const tasksTab = screen.getByRole('tab', { name: /tasks/i });
        expect(runsTab).toHaveAttribute('aria-selected', 'true');
        expect(tasksTab).toHaveAttribute('aria-selected', 'false');
        fireEvent.click(tasksTab);
        expect(mockPush).toHaveBeenCalledWith('/tasks/');
    });

    it('does not navigate when clicking the already-active tab', () => {
        render(<HistoryChrome {...baseProps} mode="runs" />);
        fireEvent.click(screen.getByRole('tab', { name: /runs/i }));
        expect(mockPush).not.toHaveBeenCalled();
    });

    it('calls onSearch as the user types', () => {
        const onSearch = vi.fn();
        render(<HistoryChrome {...baseProps} onSearch={onSearch} />);
        fireEvent.change(screen.getByLabelText('Search runs'), { target: { value: 'etl' } });
        expect(onSearch).toHaveBeenCalledWith('etl');
    });

    it('shows the current window and total, and pages forward/back', () => {
        const setPage = vi.fn();
        render(<HistoryChrome {...baseProps} page={1} setPage={setPage} />);
        expect(screen.getByText('11–20 of 25')).toBeInTheDocument();
        fireEvent.click(screen.getByLabelText('Next page'));
        expect(setPage).toHaveBeenCalledWith(2);
        fireEvent.click(screen.getByLabelText('Previous page'));
        expect(setPage).toHaveBeenCalledWith(0);
    });

    it('disables Previous on the first page and Next on the last', () => {
        const { rerender } = render(<HistoryChrome {...baseProps} page={0} />);
        expect(screen.getByLabelText('Previous page')).toBeDisabled();
        expect(screen.getByLabelText('Next page')).not.toBeDisabled();
        rerender(<HistoryChrome {...baseProps} page={2} />);
        expect(screen.getByLabelText('Next page')).toBeDisabled();
    });

    it('hides the pagination footer entirely when empty', () => {
        render(<HistoryChrome {...baseProps} total={0} pageCount={1} />);
        expect(screen.queryByText(/of 0/)).not.toBeInTheDocument();
        expect(screen.queryByLabelText('Next page')).not.toBeInTheDocument();
    });

    it('shows the count but no pager when everything fits one page', () => {
        render(<HistoryChrome {...baseProps} total={5} pageCount={1} />);
        expect(screen.getByText('5 runs')).toBeInTheDocument();
        expect(screen.queryByLabelText('Next page')).not.toBeInTheDocument();
    });

    it('uses a roving tabindex on the toggle (only the active tab is focusable)', () => {
        render(<HistoryChrome {...baseProps} mode="runs" />);
        expect(screen.getByRole('tab', { name: /runs/i })).toHaveAttribute('tabindex', '0');
        expect(screen.getByRole('tab', { name: /tasks/i })).toHaveAttribute('tabindex', '-1');
    });
    // ──────────────────────────────────────────────────────────────────────
    // Cursor paging. `total` counts what is loaded; `hasMore` is the API's word
    // for "there is older data" — together they make the count honest.
    // ──────────────────────────────────────────────────────────────────────

    it('marks the count as partial while the feed has older rows', () => {
        render(<HistoryChrome {...baseProps} hasMore />);
        expect(screen.getByText('25+ runs')).toBeInTheDocument();
    });

    it('states the count plainly once the feed is exhausted', () => {
        render(<HistoryChrome {...baseProps} hasMore={false} />);
        expect(screen.getByText('25 runs')).toBeInTheDocument();
    });

    it('loads the older page on demand', () => {
        const onLoadMore = vi.fn();
        render(<HistoryChrome {...baseProps} hasMore onLoadMore={onLoadMore} />);
        fireEvent.click(screen.getByRole('button', { name: 'Show older runs' }));
        expect(onLoadMore).toHaveBeenCalled();
    });

    it('offers nothing to load when nothing older exists', () => {
        render(<HistoryChrome {...baseProps} hasMore={false} />);
        expect(screen.queryByRole('button', { name: /show older/i })).not.toBeInTheDocument();
    });

    it('labels the button after the mode it is paging', () => {
        render(<HistoryChrome {...baseProps} mode="tasks" hasMore />);
        expect(screen.getByRole('button', { name: 'Show older tasks' })).toBeInTheDocument();
    });

    it('disables the button while the older page is in flight', () => {
        render(<HistoryChrome {...baseProps} hasMore isLoadingMore />);
        expect(screen.getByRole('button', { name: 'Loading…' })).toBeDisabled();
    });

    it('keeps the footer reachable when a search hides every loaded row', () => {
        // 0 matches but more pages behind them: hiding the footer here would strand
        // the user on "no matches" with no way to search deeper.
        render(<HistoryChrome {...baseProps} total={0} pageCount={1} hasMore />);
        expect(screen.getByText('0+ runs')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Show older runs' })).toBeInTheDocument();
    });

    it('still hides the footer when the feed is genuinely empty', () => {
        render(<HistoryChrome {...baseProps} total={0} pageCount={1} hasMore={false} />);
        expect(screen.queryByText(/runs$/)).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: /show older/i })).not.toBeInTheDocument();
    });

    it('shows the loading spinner instead of a count while the first page loads', () => {
        const { container } = render(<HistoryChrome {...baseProps} loading hasMore />);
        expect(container.querySelector('.loading-spinner-sm')).toBeTruthy();
        expect(screen.queryByRole('button', { name: /show older/i })).not.toBeInTheDocument();
    });

    // ──────────────────────────────────────────────────────────────────────
    // The footer's flex row used to lay out via `justify-content: space-between`
    // across however many of {counter, pager, button} happened to be rendered.
    // Stable at 3 children while the feed still had more to load; once `hasMore`
    // flips false the button disappears, leaving 2 — `space-between` redistributes
    // *those* to the two edges, so the pager visibly jumped from wherever it sat
    // (somewhere in the middle) to the far edge the button used to occupy the
    // instant a "Show older" click exhausted the feed. Counter and pager are now a
    // single `.hc-pagination-info` unit whose own internal layout never depends on
    // whether a sibling exists — these tests pin that unit's position and contents
    // stay identical with and without the button, rather than asserting on computed
    // CSS (jsdom has no real box model to check `space-between` math against).
    // ──────────────────────────────────────────────────────────────────────
    describe('pagination footer does not reposition when "Show older" disappears', () => {
        it('keeps the counter+pager group as the first element, whether or not the button renders', () => {
            const { container: withButton } = render(<HistoryChrome {...baseProps} hasMore />);
            const { container: withoutButton } = render(<HistoryChrome {...baseProps} hasMore={false} />);

            const infoWith = withButton.querySelector('.hc-pagination')?.firstElementChild;
            const infoWithout = withoutButton.querySelector('.hc-pagination')?.firstElementChild;

            expect(infoWith).toHaveClass('hc-pagination-info');
            expect(infoWithout).toHaveClass('hc-pagination-info');
            // Same shape in both cases (counter + pager both present) — the group's own
            // structure is untouched by whether a completely separate sibling (the
            // button) exists. Text isn't compared: the counter legitimately reads
            // differently ("25+" vs "25") since it's honestly reporting `hasMore` itself.
            expect(infoWith?.querySelector('.hc-pager')).toBeTruthy();
            expect(infoWithout?.querySelector('.hc-pager')).toBeTruthy();
        });

        it('holds the counter and pager together inside one group, not as separate flex siblings', () => {
            const { container } = render(<HistoryChrome {...baseProps} hasMore />);
            const info = container.querySelector('.hc-pagination-info');
            expect(info?.querySelector('.text-muted')).toBeTruthy();   // the counter span
            expect(info?.querySelector('.hc-pager')).toBeTruthy();     // the client pager
        });

        it('renders the button as a direct sibling of the info group, not nested inside it', () => {
            const { container } = render(<HistoryChrome {...baseProps} hasMore />);
            const pagination = container.querySelector('.hc-pagination');
            const button = screen.getByRole('button', { name: 'Show older runs' });
            expect(button.parentElement).toBe(pagination);
            expect(container.querySelector('.hc-pagination-info')?.contains(button)).toBe(false);
        });
    });
});
