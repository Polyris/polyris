import React, { useState, useMemo } from 'react';
import { Button } from '@/components/ui/button';
import { Copy, CheckCircle2 } from '../../utils/icons';

interface Props {
    value: unknown;
    ariaLabel: string;
    /**
     * When the pretty-printed JSON has more lines than this, the block starts
     * collapsed with a "Show all (N lines)" toggle. Small inputs render at
     * their natural height with no scrollbar or toggle. Default 30.
     */
    collapseAtLines?: number;
}

/**
 * Pretty-prints a JSON value with a top-right copy button and (for long
 * content) a "Show all (N lines)" expander. Mirrors the ErrorDisplay pattern
 * used by the Details tab so Input / Output blocks behave consistently.
 */
export const CollapsibleJsonBlock: React.FC<Props> = ({
    value,
    ariaLabel,
    collapseAtLines = 30,
}) => {
    const [expanded, setExpanded] = useState(false);
    const [copied, setCopied] = useState(false);

    const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
    const lineCount = useMemo(() => text.split('\n').length, [text]);
    const isLong = lineCount > collapseAtLines;
    const collapsed = isLong && !expanded;

    const handleCopy = () => {
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
    };

    return (
        <div className="td-json-block">
            <div className="td-json-block-actions">
                {copied && <span className="td-copy-feedback">Copied!</span>}
                <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 opacity-60 hover:opacity-100"
                    onClick={handleCopy}
                    title="Copy JSON"
                    aria-label={copied ? 'Copied' : 'Copy JSON'}
                >
                    {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                </Button>
            </div>
            <pre
                className="td-output-json td-json-block-pre"
                aria-label={ariaLabel}
                style={
                    collapsed
                        ? { maxHeight: '300px', overflow: 'hidden' }
                        : expanded
                            ? { maxHeight: '60vh', overflow: 'auto' }
                            : undefined
                }
            >
                {text}
            </pre>
            {isLong && (
                <button
                    type="button"
                    className="btn-link text-xs mt-1"
                    onClick={() => setExpanded(!expanded)}
                >
                    {expanded ? 'Show less' : `Show all (${lineCount} lines)`}
                </button>
            )}
        </div>
    );
};
