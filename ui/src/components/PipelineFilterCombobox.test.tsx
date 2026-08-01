/**
 * PipelineFilterCombobox tests (v0.78.7).
 *
 * Covers:
 *   - Renders input + chevron + clear (when value set)
 *   - Click input opens dropdown listing pipelines
 *   - Typing filters dropdown options (substring match)
 *   - Click option calls onChange with exact name
 *   - Clear button resets to ''
 *   - Arrow keys + Enter navigate options
 *   - Esc closes dropdown without clearing
 *   - Max 20 options with "+N more" hint when over the limit
 *   - Click outside closes dropdown
 */

import React, { createRef } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('@/utils/icons', () => ({
    ChevronDown: () => <span data-testid="icon-chevron" />,
    X: () => <span data-testid="icon-x" />,
}));

import { PipelineFilterCombobox } from './PipelineFilterCombobox';

const SHORT_LIST = [
    { name: 'acme-daily' },
    { name: 'shopmart-weekly' },
    { name: 'finance-monthly' },
];

function renderCombobox(overrides: Partial<React.ComponentProps<typeof PipelineFilterCombobox>> = {}) {
    const onChange = overrides.onChange ?? vi.fn();
    const utils = render(
        <PipelineFilterCombobox
            value={overrides.value ?? ''}
            onChange={onChange}
            pipelines={overrides.pipelines ?? SHORT_LIST}
            placeholder={overrides.placeholder}
        />
    );
    return { ...utils, onChange };
}

describe('PipelineFilterCombobox', () => {
    it('renders input with placeholder and chevron toggle', () => {
        renderCombobox();
        const input = screen.getByRole('combobox');
        expect(input).toBeInTheDocument();
        expect(screen.getByTestId('icon-chevron')).toBeInTheDocument();
    });

    it('does not show clear button when value is empty', () => {
        renderCombobox({ value: '' });
        // Clear button has aria-label "Clear filter"; should not exist
        expect(screen.queryByLabelText('Clear filter')).toBeNull();
    });

    it('shows clear button when value is non-empty', () => {
        renderCombobox({ value: 'acme' });
        expect(screen.queryByLabelText('Clear filter')).not.toBeNull();
    });

    it('opens dropdown on focus and lists all pipelines (empty filter)', () => {
        renderCombobox();
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        expect(screen.getByText('acme-daily')).toBeInTheDocument();
        expect(screen.getByText('shopmart-weekly')).toBeInTheDocument();
        expect(screen.getByText('finance-monthly')).toBeInTheDocument();
    });

    it('typing calls onChange with input value', () => {
        const { onChange } = renderCombobox();
        const input = screen.getByRole('combobox');
        fireEvent.change(input, { target: { value: 'acme' } });
        expect(onChange).toHaveBeenCalledWith('acme');
    });

    it('filters dropdown options by substring of value', () => {
        renderCombobox({ value: 'monthly' });
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        // Only finance-monthly matches; acme-daily and shopmart-weekly should be filtered out
        expect(screen.queryByText('finance-monthly')).toBeInTheDocument();
        expect(screen.queryByText('acme-daily')).toBeNull();
        expect(screen.queryByText('shopmart-weekly')).toBeNull();
    });

    it('clicking an option calls onChange with exact name and closes dropdown', () => {
        const { onChange } = renderCombobox();
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        fireEvent.click(screen.getByText('acme-daily'));
        expect(onChange).toHaveBeenCalledWith('acme-daily');
        // Dropdown should be closed — options no longer rendered
        expect(screen.queryByText('shopmart-weekly')).toBeNull();
    });

    it('clear button resets to empty string', () => {
        const { onChange } = renderCombobox({ value: 'acme' });
        const clearBtn = screen.getByLabelText('Clear filter');
        fireEvent.click(clearBtn);
        expect(onChange).toHaveBeenCalledWith('');
    });

    it('Esc closes the dropdown without clearing the filter', () => {
        const { onChange } = renderCombobox({ value: 'acme' });
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        expect(screen.queryByText('acme-daily')).toBeInTheDocument();
        fireEvent.keyDown(input, { key: 'Escape' });
        // Dropdown closed
        expect(screen.queryByText('acme-daily')).toBeNull();
        // But onChange was NOT called — value preserved
        expect(onChange).not.toHaveBeenCalled();
    });

    it('shows empty message when filter matches nothing', () => {
        renderCombobox({ value: 'no-such-pipeline' });
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        expect(screen.queryByText(/No pipelines match "no-such-pipeline"/)).toBeInTheDocument();
    });

    it('shows "+N more" hint when matches exceed 20', () => {
        const many = Array.from({ length: 25 }, (_, i) => ({ name: `pipeline-${i.toString().padStart(2, '0')}` }));
        renderCombobox({ pipelines: many });
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        // Hint shows count over the visible cap
        expect(screen.queryByText(/\+5 more/)).toBeInTheDocument();
    });

    it('ArrowDown then Enter selects the first option', () => {
        const { onChange } = renderCombobox();
        const input = screen.getByRole('combobox');
        fireEvent.focus(input);
        fireEvent.keyDown(input, { key: 'ArrowDown' });
        fireEvent.keyDown(input, { key: 'Enter' });
        expect(onChange).toHaveBeenCalledWith('acme-daily');
    });

    it('forwards the input ref so parent can focus via shortcut', () => {
        const inputRef = createRef<HTMLInputElement>();
        render(
            <PipelineFilterCombobox
                value=""
                onChange={vi.fn()}
                pipelines={SHORT_LIST}
                inputRef={inputRef}
            />
        );
        expect(inputRef.current).not.toBeNull();
        expect(inputRef.current?.tagName).toBe('INPUT');
    });
});
