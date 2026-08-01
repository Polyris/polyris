import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DatePicker } from './DatePicker';

vi.mock('../utils/icons', () => ({
    Calendar: () => <span data-testid="icon-calendar" />,
    ChevronLeft: () => <span data-testid="icon-left" />,
    ChevronRight: () => <span data-testid="icon-right" />,
}));

describe('DatePicker', () => {
    it('shows the formatted value on the trigger', () => {
        render(<DatePicker value="2026-07-09" onChange={() => {}} />);
        expect(screen.getByText('Jul 9, 2026')).toBeInTheDocument();
    });

    it('shows the placeholder when empty', () => {
        render(<DatePicker value="" onChange={() => {}} placeholder="All dates" />);
        expect(screen.getByText('All dates')).toBeInTheDocument();
    });

    it('opens the calendar and selects a day', () => {
        const onChange = vi.fn();
        render(<DatePicker value="2026-07-09" onChange={onChange} />);
        fireEvent.click(screen.getByRole('button', { name: /select date/i }));
        // Calendar dialog is open with the month label.
        expect(screen.getByText('July 2026')).toBeInTheDocument();
        // Pick day 15.
        fireEvent.click(screen.getByRole('button', { name: '15' }));
        expect(onChange).toHaveBeenCalledWith('2026-07-15');
    });

    it('clears the value via Clear', () => {
        const onChange = vi.fn();
        render(<DatePicker value="2026-07-09" onChange={onChange} />);
        fireEvent.click(screen.getByRole('button', { name: /select date/i }));
        fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
        expect(onChange).toHaveBeenCalledWith('');
    });

    it('hides Clear when allowClear is false', () => {
        render(<DatePicker value="2026-07-09" onChange={() => {}} allowClear={false} />);
        fireEvent.click(screen.getByRole('button', { name: /select date/i }));
        expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Today' })).toBeInTheDocument();
    });
});
