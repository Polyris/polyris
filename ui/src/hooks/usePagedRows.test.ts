import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePagedRows } from './usePagedRows';

type Row = { name: string };
const rows: Row[] = Array.from({ length: 25 }, (_, i) => ({ name: `row-${i}` }));
const getText = (r: Row) => r.name;

describe('usePagedRows', () => {
    it('paginates with the given page size', () => {
        const { result } = renderHook(() => usePagedRows(rows, '', getText, 10));
        expect(result.current.total).toBe(25);
        expect(result.current.pageCount).toBe(3);
        expect(result.current.paged).toHaveLength(10);
        expect(result.current.paged[0].name).toBe('row-0');
    });

    it('returns the correct slice for a later page', () => {
        const { result } = renderHook(() => usePagedRows(rows, '', getText, 10));
        act(() => result.current.setPage(2));
        expect(result.current.page).toBe(2);
        expect(result.current.paged).toHaveLength(5); // 20..24
        expect(result.current.paged[0].name).toBe('row-20');
    });

    it('filters by the search text over the projected field', () => {
        const { result } = renderHook(() => usePagedRows(rows, 'row-1', getText, 10));
        // row-1, row-10..row-19 → 11 matches
        expect(result.current.total).toBe(11);
        expect(result.current.paged.every(r => r.name.includes('row-1'))).toBe(true);
    });

    it('is case-insensitive and trims the query', () => {
        const { result } = renderHook(() => usePagedRows([{ name: 'ACME' }], '  acme  ', getText, 10));
        expect(result.current.total).toBe(1);
    });

    it('resets to the first page when the search changes', () => {
        const { result, rerender } = renderHook(
            ({ q }: { q: string }) => usePagedRows(rows, q, getText, 10),
            { initialProps: { q: '' } },
        );
        act(() => result.current.setPage(2));
        expect(result.current.page).toBe(2);
        rerender({ q: 'row-2' });
        expect(result.current.page).toBe(0);
    });

    it('clamps the page into range when the filtered set shrinks', () => {
        const { result, rerender } = renderHook(
            ({ q }: { q: string }) => usePagedRows(rows, q, getText, 10),
            { initialProps: { q: '' } },
        );
        act(() => result.current.setPage(2));
        // Narrow to a single match — only one page remains, page must clamp to 0.
        rerender({ q: 'row-7' });
        expect(result.current.page).toBe(0);
        expect(result.current.paged).toHaveLength(1);
    });
});
