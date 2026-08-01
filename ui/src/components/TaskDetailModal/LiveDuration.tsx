import { useState, useEffect } from 'react';
import { formatDuration } from '../../utils';
import { MS } from '../../utils/constants';
import type { LiveDurationProps } from '@/types';

/**
 * LiveDuration - Shows live updating duration for running tasks
 * Uses running_at (when task actually started running) not started_at (wrapper start)
 */
export const LiveDuration = ({ task }: LiveDurationProps) => {
    const [tick, setTick] = useState(() => Date.now());
    const status = task?.status;
    const isRunning = status === 'running';
    
    useEffect(() => {
        if (!isRunning) return;
        const interval = setInterval(() => setTick(Date.now()), MS.TICK_INTERVAL);
        return () => clearInterval(interval);
    }, [isRunning]);
    
    // Use running_at (actual task start) not started_at (wrapper start)
    const runningAt = task?.running_at;
    
    // Not started running yet
    if (!runningAt) {
        return '--';
    }
    
    // Has finished_at - show final duration
    if (task.finished_at) {
        return formatDuration(new Date(task.finished_at).getTime() - new Date(runningAt).getTime());
    }
    
    // Running - show live duration
    if (isRunning) {
        return formatDuration(tick - new Date(runningAt).getTime());
    }
    
    // Terminal state without finished_at
    return '--';
};
