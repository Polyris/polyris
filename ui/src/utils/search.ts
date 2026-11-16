/**
 * True when every whitespace-separated term of `search` is in `text`, in any
 * order, case-insensitive. An empty search matches everything.
 *
 * The one matcher for pipeline names: the sidebar's search and the command
 * palette (⌘K) both use it, so the same words find the same pipelines.
 */
export function matchesSearch(text: string, search: string): boolean {
    const terms = search.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const lower = text.toLowerCase();
    return terms.every(term => lower.includes(term));
}
