import { useMemo, useState } from 'react';

/**
 * Client-side search + pagination over an already-filtered/sorted list.
 *
 * Composition order matters: callers sort (and store-filter) first, then feed the result here,
 * which applies the free-text search and then paginates. The page resets to 0 whenever the
 * search changes (React "adjust state during render" pattern — no effect), and the returned
 * page is always clamped into range so the slice can't fall off the end.
 */
export function usePagedRows<T>(
    rows: T[],
    search: string,
    getSearchText: (row: T) => string,
    pageSize = 10,
) {
    const [page, setPage] = useState(0);
    const [prevSearch, setPrevSearch] = useState(search);

    const filtered = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return rows;
        return rows.filter(r => getSearchText(r).toLowerCase().includes(q));
    }, [rows, search, getSearchText]);

    // Reset to the first page when the search term changes, without an effect.
    if (search !== prevSearch) {
        setPrevSearch(search);
        setPage(0);
    }

    const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
    const safePage = Math.min(page, pageCount - 1);

    const paged = useMemo(
        () => filtered.slice(safePage * pageSize, safePage * pageSize + pageSize),
        [filtered, safePage, pageSize],
    );

    return { paged, page: safePage, setPage, total: filtered.length, pageCount, pageSize };
}
