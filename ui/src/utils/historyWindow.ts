/** Without a date, /api/runs and /api/tasks read the run rows of the last RUNS_WINDOW_DAYS days
 *  by the day each run started, with a pipeline filter or without (console_api
 *  `Limits.RUNS_WINDOW_DAYS`, run-start-index, ADR #137 §5); the Tasks tab then reads the tasks
 *  of the runs it shows (D140). A chosen date has no window. */
export const RUNS_WINDOW_DAYS = 90;

/** /api/pipelines?stats=true reads the runs of the last RECENT_WINDOW_DAYS start days for each
 *  pipeline's `status` and `recent_runs`, which the pipelines sidebar shows (console_api
 *  `Limits.SLA_DAYS`). */
export const RECENT_WINDOW_DAYS = 14;

/** The two halves of History. */
export type HistoryNoun = 'runs' | 'tasks';

/** The days a History tab reads while its window applies: both read run rows (D140). */
export const historyWindowDays = (_noun: HistoryNoun): number => RUNS_WINDOW_DAYS;

/** Whether a History tab's read is narrowed to its window: unless a date is chosen. */
export const isHistoryWindowed = (_noun: HistoryNoun, filter: { pipeline?: string; date?: string }): boolean =>
    !filter.date;

/** What the date filter shows while it is empty. */
export const historyDatePlaceholder = (noun: HistoryNoun, windowed: boolean): string =>
    windowed ? `Last ${historyWindowDays(noun)} days` : 'All dates';

/** The empty state of a History tab: says which days were searched when the window applies. */
export function historyEmptyState(
    noun: HistoryNoun,
    filtered: boolean,
    windowed: boolean,
): { title: string; description: string } {
    const days = historyWindowDays(noun);
    const widen = 'a date';
    if (filtered) {
        return {
            title: `No matching ${noun}`,
            description: windowed
                ? `Try adjusting your filters or search. Without ${widen}, only the last ${days} days are searched.`
                : 'Try adjusting your filters or search',
        };
    }
    return {
        title: `No ${noun} in the last ${days} days`,
        description: `To see older ${noun}, pick ${widen}.`,
    };
}
