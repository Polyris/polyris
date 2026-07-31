import { describe, it, expect } from 'vitest';
import { STATUS_COLORS } from './constants';
import { TASK_STATUS } from '@/generated/enums';
import type { ExecutionStatus } from '@/generated/enums';

/**
 * Drift guard: the History pills and StatusIcon derive their colour from STATUS_COLORS, so
 * every status the backend can emit must have a tone here — otherwise it renders uncoloured.
 * ExecutionStatus has no runtime const in generated/enums.ts (type only), so it is mirrored
 * here as a typed array; tsc rejects any value that isn't a real ExecutionStatus.
 */
const EXECUTION_STATUSES: ExecutionStatus[] = [
    'running', 'success', 'failed', 'timed_out', 'aborted', 'recovered',
];

describe('status colour coverage (drift guard)', () => {
    it('every TaskStatus has a STATUS_COLORS tone', () => {
        for (const status of Object.values(TASK_STATUS)) {
            expect(STATUS_COLORS, `no tone for task status "${status}"`).toHaveProperty(status);
        }
    });

    it('every ExecutionStatus has a STATUS_COLORS tone', () => {
        for (const status of EXECUTION_STATUSES) {
            expect(STATUS_COLORS, `no tone for run status "${status}"`).toHaveProperty(status);
        }
    });
});
