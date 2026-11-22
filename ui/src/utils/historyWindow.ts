/** Without a date, /api/runs reads the run rows of the last RUNS_WINDOW_DAYS days by the
 *  day each run started, with a pipeline filter or without (console_api
 *  `Limits.RUNS_WINDOW_DAYS`, run-start-index, ADR #137 §5). A chosen date has no window. */
export const RUNS_WINDOW_DAYS = 90;

/** Without a pipeline or a date, /api/tasks reads only the last RECENT_WINDOW_DAYS days, one
 *  date at a time (console_api `Limits.SLA_DAYS`, feed.py, ADR #108); a chosen pipeline or
 *  date has no window. /api/pipelines?stats=true reads the runs of as many start days for
 *  each pipeline's `status` and `recent_runs`, which the pipelines sidebar shows. */
export const RECENT_WINDOW_DAYS = 14;

/** The two halves of History. */
export type HistoryNoun = 'runs' | 'tasks';

/** The days a History tab reads while its window applies. */
export const historyWindowDays = (noun: HistoryNoun): number =>
    noun === 'runs' ? RUNS_WINDOW_DAYS : RECENT_WINDOW_DAYS;

/** Whether a History tab's read is narrowed to its window: Runs unless a date is chosen,
 *  Tasks unless a pipeline or a date is. */
export const isHistoryWindowed = (noun: HistoryNoun, filter: { pipeline?: string; date?: string }): boolean =>
    noun === 'runs' ? !filter.date : !filter.pipeline && !filter.date;

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
    const widen = noun === 'runs' ? 'a date' : 'a pipeline or a date';
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
