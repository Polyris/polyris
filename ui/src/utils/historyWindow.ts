/** Without a pipeline or a date, /api/runs and /api/tasks read only the last
 *  HISTORY_WINDOW_DAYS days, one date at a time (console_api `Limits.SLA_DAYS`,
 *  feed.py, ADR #108). A chosen pipeline or date has no window. */
export const HISTORY_WINDOW_DAYS = 14;

export const isHistoryWindowed = (filter: { pipeline?: string; date?: string }): boolean =>
    !filter.pipeline && !filter.date;

/** What the date filter shows while it is empty. */
export const historyDatePlaceholder = (windowed: boolean): string =>
    windowed ? `Last ${HISTORY_WINDOW_DAYS} days` : 'All dates';

/** The empty state of a History tab: says which days were searched when the window applies. */
export function historyEmptyState(
    noun: 'runs' | 'tasks',
    filtered: boolean,
    windowed: boolean,
): { title: string; description: string } {
    if (filtered) {
        return {
            title: `No matching ${noun}`,
            description: windowed
                ? `Try adjusting your filters or search. Without a pipeline or a date, only the last ${HISTORY_WINDOW_DAYS} days are searched.`
                : 'Try adjusting your filters or search',
        };
    }
    return {
        title: `No ${noun} in the last ${HISTORY_WINDOW_DAYS} days`,
        description: `To see older ${noun}, pick a pipeline or a date.`,
    };
}
