import React, { useState } from 'react';
import type { TaskError } from '@/types';

/**
 * ErrorDisplay - Formats error text as collapsible JSON or traceback
 */
export const ErrorDisplay = ({ error }: { error: TaskError | string | null }) => {
    const [expanded, setExpanded] = useState(false);
    
    if (!error) return null;
    
    const errorStr = typeof error === 'string' ? error : JSON.stringify(error);
    
    // Try to parse as JSON
    let parsed = null;
    let isJson = false;
    try {
        parsed = JSON.parse(errorStr);
        isJson = true;
    } catch {
        // Not JSON - check if it's a nested JSON string (common with Step Functions)
        try {
            // Sometimes error.Cause is a JSON string inside a JSON string
            if (errorStr.includes('"Cause"')) {
                parsed = JSON.parse(errorStr);
                if (parsed.Cause && typeof parsed.Cause === 'string') {
                    try { parsed.Cause = JSON.parse(parsed.Cause); } catch { /* nested JSON parse may fail */ }
                }
                isJson = true;
            }
        } catch { /* not JSON, display as-is */ }
    }
    
    const isLong = errorStr.length > 300;
    const displayText = isJson ? JSON.stringify(parsed, null, 2) : errorStr;
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
