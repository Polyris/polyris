import React, { useState } from 'react';
import { Button } from '@/components/ui/button';
import { ChevronDown, ChevronRight, Copy, CheckCircle2 } from '../../utils/icons';

interface Props {
    label: string;
    /** Rendered as "({count})" next to the label. Omit to hide. */
    count?: number;
    /** Whether the section starts expanded. Default: false (collapsed). */
    defaultOpen?: boolean;
    /**
     * If provided, the header shows a copy button that copies
     * `JSON.stringify(copyValue, null, 2)` — works even while collapsed.
     */
    copyValue?: unknown;
    /** Optional aria-label for the copy button. Defaults to "Copy {label}". */
    copyAriaLabel?: string;
    children: React.ReactNode;
}

/**
 * Uniform collapsible section for the Task Detail Input/Output tab. Header
 * row is always visible with a chevron + optional count + optional copy
 * button; body renders only when expanded. Same interaction model across
 * Variables / Output so users learn it once.
 *
 * Header uses role="button" (not <button>) so the copy affordance can be a
 * real nested <button> — nested interactive elements are invalid otherwise.
 */
export const CollapsibleSection: React.FC<Props> = ({
    label,
    count,
    defaultOpen = false,
    copyValue,
    copyAriaLabel,
    children,
}) => {
    const [open, setOpen] = useState(defaultOpen);
    const [copied, setCopied] = useState(false);

    const toggle = () => setOpen(!open);
    const handleKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggle();
        }
    };

    const handleCopy = (e: React.MouseEvent) => {
        e.stopPropagation(); // don't toggle the section when clicking copy
        if (copyValue === undefined) return;
        const text = typeof copyValue === 'string'
            ? copyValue
            : JSON.stringify(copyValue, null, 2);
        navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
    };

    return (
        <div className="td-collapsible-section">
            <div
                role="button"
                tabIndex={0}
                className="td-collapsible-header"
                onClick={toggle}
                onKeyDown={handleKeyDown}
                aria-expanded={open}
            >
                {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <span className="td-collapsible-label">{label}</span>
                {count !== undefined && (
                    <span className="td-collapsible-count">({count})</span>
                )}
                <div className="td-collapsible-actions">
                    {copied && <span className="td-copy-feedback">Copied!</span>}
                    {copyValue !== undefined && (
                        <Button
                            variant="ghost"
                            size="icon"
                            className="h-6 w-6 opacity-60 hover:opacity-100"
                            onClick={handleCopy}
                            title={copyAriaLabel ?? `Copy ${label}`}
                            aria-label={copied ? 'Copied' : (copyAriaLabel ?? `Copy ${label}`)}
                        >
                            {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                        </Button>
                    )}
                </div>
            </div>
            {open && <div className="td-collapsible-body">{children}</div>}
        </div>
    );
};
