import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import { Calendar, ChevronLeft, ChevronRight } from '../utils/icons';

export interface DatePickerProps {
    /** Selected date as YYYY-MM-DD, or '' for no date. */
    value: string;
    /** Called with the new YYYY-MM-DD, or '' when cleared. */
    onChange: (value: string) => void;
    className?: string;
    /** Shown on the trigger when value is empty. */
    placeholder?: string;
    /** Show the Clear button (default true). */
    allowClear?: boolean;
    ariaLabel?: string;
}

const WEEKDAYS = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];
const MONTHS = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
];

function toYMD(d: Date): string {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
}

function parseYMD(s: string): Date | null {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
    if (!m) return null;
    return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

function formatDisplay(s: string): string {
    const d = parseYMD(s);
    if (!d) return '';
    return `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${d.getFullYear()}`;
}

/**
 * App-styled date picker — a single reusable control that replaces the native
 * `<input type="date">` everywhere so every calendar in the app looks identical.
 * Controlled: `value` is YYYY-MM-DD (or '' for "no date"), `onChange` emits the same.
 */
export function DatePicker({
    value,
    onChange,
    className = '',
    placeholder = 'All dates',
    allowClear = true,
    ariaLabel = 'Select date',
}: DatePickerProps) {
    const [open, setOpen] = useState(false);
    const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
    const [viewMonth, setViewMonth] = useState<Date>(() => parseYMD(value) || new Date());
    const triggerRef = useRef<HTMLButtonElement>(null);
    const popupRef = useRef<HTMLDivElement>(null);

    // Show the month of the current value each time the picker opens, and anchor the
    // popup to the trigger (rendered in a portal, so it escapes clipping containers).
    const toggleOpen = () => {
        if (!open) {
            setViewMonth(parseYMD(value) || new Date());
            const r = triggerRef.current?.getBoundingClientRect();
            if (r) {
                // Keep the 260px popup inside the viewport. The picker is the
                // right-most control in some toolbars, so anchoring at r.left would
                // push the calendar off the right edge — clamp instead.
                const POPUP_W = 260;  // matches .dp-popup width in CSS
                const left = Math.min(r.left, window.innerWidth - POPUP_W - 8);
                setPos({ top: r.bottom + 4, left: Math.max(8, left) });
            }
        }
        setOpen(o => !o);
    };

    // Close on outside click (checking both trigger and the portaled popup) and on scroll.
    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            const t = e.target as Node;
            if (triggerRef.current?.contains(t)) return;
            if (popupRef.current?.contains(t)) return;
            setOpen(false);
        };
        const onScroll = () => setOpen(false);
        document.addEventListener('mousedown', onDown);
        window.addEventListener('scroll', onScroll, true);
        return () => {
            document.removeEventListener('mousedown', onDown);
            window.removeEventListener('scroll', onScroll, true);
        };
    }, [open]);

    const todayYMD = toYMD(new Date());
    const year = viewMonth.getFullYear();
    const month = viewMonth.getMonth();
    const firstDow = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    const cells: (number | null)[] = [];
    for (let i = 0; i < firstDow; i++) cells.push(null);
    for (let d = 1; d <= daysInMonth; d++) cells.push(d);

    const pick = (d: number) => {
        onChange(toYMD(new Date(year, month, d)));
        setOpen(false);
    };

    return (
        <div className={`dp ${className}`.trim()}>
            <button
                ref={triggerRef}
                type="button"
                className="dp-trigger"
                onClick={toggleOpen}
                aria-label={ariaLabel}
                aria-expanded={open}
                aria-haspopup="dialog"
            >
                <span className={value ? 'dp-value' : 'dp-placeholder'}>
                    {value ? formatDisplay(value) : placeholder}
                </span>
                <Calendar size={16} />
            </button>

            {open && pos && createPortal(
                <div
                    ref={popupRef}
                    className="dp-popup"
                    style={{ position: 'fixed', top: pos.top, left: pos.left }}
                    role="dialog"
                    aria-label="Choose date"
                >
                    <div className="dp-head">
                        <button
                            type="button"
                            className="dp-nav"
                            onClick={() => setViewMonth(new Date(year, month - 1, 1))}
                            aria-label="Previous month"
                        >
                            <ChevronLeft size={16} />
                        </button>
                        <span className="dp-month">{MONTHS[month]} {year}</span>
                        <button
                            type="button"
                            className="dp-nav"
                            onClick={() => setViewMonth(new Date(year, month + 1, 1))}
                            aria-label="Next month"
                        >
                            <ChevronRight size={16} />
                        </button>
                    </div>

                    <div className="dp-weekdays">
                        {WEEKDAYS.map(w => <span key={w} className="dp-weekday">{w}</span>)}
                    </div>

                    <div className="dp-grid">
                        {cells.map((d, i) => {
                            if (d === null) return <span key={i} className="dp-cell dp-cell--empty" />;
                            const ymd = toYMD(new Date(year, month, d));
                            const isSelected = !!value && ymd === value;
                            const isToday = !isSelected && ymd === todayYMD;
                            return (
                                <button
                                    key={i}
                                    type="button"
                                    className={`dp-cell${isSelected ? ' dp-cell--selected' : ''}${isToday ? ' dp-cell--today' : ''}`}
                                    onClick={() => pick(d)}
                                    aria-pressed={isSelected}
                                >
                                    {d}
                                </button>
                            );
                        })}
                    </div>

                    <div className="dp-foot">
                        {allowClear && (
                            <button
                                type="button"
                                className="dp-foot-btn"
                                onClick={() => { onChange(''); setOpen(false); }}
                            >
                                Clear
                            </button>
                        )}
                        <button
                            type="button"
                            className="dp-foot-btn"
                            onClick={() => { onChange(todayYMD); setOpen(false); }}
                        >
                            Today
                        </button>
                    </div>
                </div>,
                document.body
            )}
        </div>
    );
}
