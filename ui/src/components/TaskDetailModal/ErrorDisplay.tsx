import React, { useState } from 'react';
import type { TaskError } from '@/types';

/** Serializes a TaskError for display: strips empty fields, expands nested Cause JSON, pretty-prints. */
export function serializeErrorForDisplay(error: TaskError | string | null): string {
    if (!error) return '';
    const compact = typeof error === 'string'
        ? error
        : JSON.stringify(Object.fromEntries(Object.entries(error).filter(([, v]) => v !== undefined && v !== '')));
    try {
        let parsed = JSON.parse(compact);
        if (parsed?.Cause && typeof parsed.Cause === 'string') {
            try { parsed = { ...parsed, Cause: JSON.parse(parsed.Cause) }; } catch { /* Cause is not JSON */ }
        }
        return JSON.stringify(parsed, null, 2);
    } catch {
        return compact;
    }
}

/**
 * ErrorDisplay - Formats error text as collapsible JSON or traceback
 */
export const ErrorDisplay = ({ error }: { error: TaskError | string | null }) => {
    const [expanded, setExpanded] = useState(false);

    if (!error) return null;

    const displayText = serializeErrorForDisplay(error);
    const isLong = displayText.length > 300;
    const truncated = !expanded && isLong;

    return (
        <div className="td-error-display">
            <pre className="td-error-pre" style={{
                maxHeight: truncated ? '120px' : '400px',
                overflow: truncated ? 'hidden' : 'auto',
                position: 'relative',
            }}>
                {truncated ? displayText.slice(0, 300) + '...' : displayText}
            </pre>
            {isLong && (
                <button
                    className="btn-link text-xs mt-1"
                    onClick={() => setExpanded(!expanded)}
                >
                    {expanded ? 'Show less' : `Show all (${displayText.split('\n').length} lines)`}
                </button>
            )}
        </div>
    );
};
