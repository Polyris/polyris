/**
 * PipelineFilterCombobox — text input with a dropdown of matching pipeline
 * names. Used in BackfillsListPage filter row (v0.78.7) instead of the
 * plain `<input>` so users can pick from the known list rather than typing
 * the full name from memory.
 *
 * Design:
 *   - Behaves as a normal substring filter: typing narrows visible rows
 *     in the parent table (the parent decides what to do with `value`).
 *   - Focus or typing opens the dropdown; click outside or Esc closes.
 *   - Dropdown lists at most 20 matches with a "+N more" hint when the
 *     filter is too broad. Click an option to set the filter to the exact
 *     name. A "Clear filter" row appears when the input is non-empty.
 *
 * BEM prefix `bl-pcb-*` (BackfillsList — PipelineCombobox), to keep CSS
 * scoped and avoid colliding with the global CommandPalette styles.
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, X as XIcon } from '@/utils/icons';

interface PipelineLike {
    name: string;
}

interface PipelineFilterComboboxProps {
    /** Current filter text (controlled). */
    value: string;
    /** Called with the new filter text on type or option select. */
    onChange: (next: string) => void;
    /** Full pipeline list to choose from. */
    pipelines: PipelineLike[];
    /** Optional ref so parent can focus the input via shortcut. */
    inputRef?: React.RefObject<HTMLInputElement | null>;
    /** Placeholder shown when value is empty. */
    placeholder?: string;
}

const MAX_VISIBLE_OPTIONS = 20;

export function PipelineFilterCombobox({
    value,
    onChange,
    pipelines,
    inputRef,
    placeholder = 'Filter by pipeline name…',
}: PipelineFilterComboboxProps) {
    const [open, setOpen] = useState(false);
    const [activeIndex, setActiveIndex] = useState(-1);
    const containerRef = useRef<HTMLDivElement>(null);

    // Filter options by substring match against current value.
    const filtered = useMemo(() => {
        const needle = value.trim().toLowerCase();
        if (!needle) return pipelines;
        return pipelines.filter((p) => p.name.toLowerCase().includes(needle));
    }, [pipelines, value]);

    const visible = filtered.slice(0, MAX_VISIBLE_OPTIONS);
    const hasMore = filtered.length > MAX_VISIBLE_OPTIONS;

    // Close on click outside.
    useEffect(() => {
        if (!open) return;
        const handler = (e: MouseEvent) => {
            if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
                setOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    // Reset active index when filter changes.
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- reset active index when the filtered list changes
        setActiveIndex(-1);
    }, [value]);

    const selectOption = (name: string) => {
        onChange(name);
        setOpen(false);
        setActiveIndex(-1);
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
        // Esc closes the dropdown (does NOT clear the filter).
        if (e.key === 'Escape') {
            if (open) {
                e.preventDefault();
                setOpen(false);
                setActiveIndex(-1);
            }
            return;
        }
        // Arrow keys + Enter navigate the options list.
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            if (!open) setOpen(true);
            setActiveIndex((prev) => Math.min(prev + 1, visible.length - 1));
            return;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIndex((prev) => Math.max(prev - 1, 0));
            return;
        }
        if (e.key === 'Enter' && open && activeIndex >= 0 && visible[activeIndex]) {
            e.preventDefault();
            selectOption(visible[activeIndex].name);
            return;
        }
    };

    const handleClear = () => {
        onChange('');
        setOpen(false);
        setActiveIndex(-1);
        inputRef?.current?.focus();
    };

    return (
        <div ref={containerRef} className="bl-pcb">
            <div className="bl-pcb-input-wrap">
                <input
                    ref={inputRef}
                    type="text"
                    className="bl-pcb-input"
                    placeholder={placeholder}
                    value={value}
                    onChange={(e) => {
                        onChange(e.target.value);
                        setOpen(true);
                    }}
                    onFocus={() => setOpen(true)}
                    onKeyDown={handleKeyDown}
                    role="combobox"
                    aria-expanded={open}
                    aria-autocomplete="list"
                    aria-controls="bl-pcb-listbox"
                    aria-activedescendant={
                        activeIndex >= 0 && visible[activeIndex]
                            ? `bl-pcb-opt-${visible[activeIndex].name}`
                            : undefined
                    }
                    aria-label="Filter backfills by pipeline name"
                />
                {value && (
                    <button
                        type="button"
                        className="bl-pcb-clear"
                        onClick={handleClear}
                        aria-label="Clear filter"
                        title="Clear filter"
                    >
                        <XIcon size={14} />
                    </button>
                )}
                <button
                    type="button"
                    className="bl-pcb-toggle"
                    onClick={() => setOpen((o) => !o)}
                    aria-label={open ? 'Close pipeline list' : 'Open pipeline list'}
                    tabIndex={-1}
                >
                    <ChevronDown size={14} />
                </button>
            </div>

            {open && (
                <div
                    className="bl-pcb-dropdown"
                    role="listbox"
                    id="bl-pcb-listbox"
                    aria-label="Pipeline options"
                >
                    {visible.length === 0 && (
                        <div className="bl-pcb-empty">
                            {value
                                ? `No pipelines match "${value}"`
                                : 'No pipelines loaded yet'}
                        </div>
                    )}
                    {visible.map((p, idx) => {
                        const isActive = idx === activeIndex;
                        const isSelected = value === p.name;
                        return (
                            <button
                                key={p.name}
                                type="button"
                                id={`bl-pcb-opt-${p.name}`}
                                className={[
                                    'bl-pcb-option',
                                    isActive ? 'bl-pcb-option--active' : '',
                                    isSelected ? 'bl-pcb-option--selected' : '',
                                ].filter(Boolean).join(' ')}
                                onClick={() => selectOption(p.name)}
                                role="option"
                                aria-selected={isSelected}
                            >
                                {p.name}
                            </button>
                        );
                    })}
                    {hasMore && (
                        <div className="bl-pcb-more">
                            +{filtered.length - MAX_VISIBLE_OPTIONS} more — keep typing to narrow
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

export default PipelineFilterCombobox;
