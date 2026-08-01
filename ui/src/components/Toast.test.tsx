import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { Toast, ToastContainer, ToastProvider, useToast } from './Toast';

vi.mock('@/utils/icons', () => ({
    ToastIcons: { info: () => <span data-testid="icon-info" />, success: () => <span data-testid="icon-success" />, error: () => <span data-testid="icon-error" />, warning: () => <span data-testid="icon-warning" /> },
    X: () => <span data-testid="icon-x" />,
}));

describe('Toast', () => {
    beforeEach(() => { vi.useFakeTimers(); });

    describe('ToastContainer', () => {
        it('renders nothing when no toasts', () => {
            const { container } = render(<ToastContainer toasts={[]} onRemove={vi.fn()} />);
            expect(container.innerHTML).toBe('');
        });

        it('renders toasts with correct messages', () => {
            const toasts = [
                { id: 1, message: 'Success!', type: 'success', duration: 4000 },
                { id: 2, message: 'Error!', type: 'error', duration: 4000 },
            ];
            render(<ToastContainer toasts={toasts} onRemove={vi.fn()} />);
            expect(screen.getByText('Success!')).toBeInTheDocument();
            expect(screen.getByText('Error!')).toBeInTheDocument();
        });

        it('has correct a11y attributes', () => {
            const toasts = [{ id: 1, message: 'Test', type: 'info', duration: 4000 }];
            render(<ToastContainer toasts={toasts} onRemove={vi.fn()} />);
            const container = screen.getByRole('status');
            expect(container).toHaveAttribute('aria-live', 'polite');
        });

        it('calls onRemove when close button clicked', () => {
            const onRemove = vi.fn();
            const toasts = [{ id: 1, message: 'Test', type: 'info', duration: 4000 }];
            render(<ToastContainer toasts={toasts} onRemove={onRemove} />);
            
            const closeBtn = screen.getByLabelText('Close');
            fireEvent.click(closeBtn);
            
            // Close has 300ms animation delay
            act(() => { vi.advanceTimersByTime(300); });
            expect(onRemove).toHaveBeenCalledWith(1);
        });

        it('auto-removes toast after duration', () => {
            const onRemove = vi.fn();
            const toasts = [{ id: 1, message: 'Test', type: 'info', duration: 2000 }];
            render(<ToastContainer toasts={toasts} onRemove={onRemove} />);
            
            act(() => { vi.advanceTimersByTime(2000); });
            expect(onRemove).toHaveBeenCalledWith(1);
        });
    });

    describe('Legacy Toast', () => {
        it('renders message', () => {
            render(<Toast message="Hello" onClose={vi.fn()} />);
            expect(screen.getByText('Hello')).toBeInTheDocument();
        });

        it('applies type class', () => {
            render(<Toast message="Error" type="error" onClose={vi.fn()} />);
            expect(screen.getByRole('alert')).toHaveClass('toast-error');
        });
    });

    describe('ToastProvider + useToast', () => {
        function TestConsumer() {
            const toast = useToast();
            return (
                <div>
                    <button onClick={() => toast.success('Done!')}>Success</button>
                    <button onClick={() => toast.error('Failed!')}>Error</button>
                    <button onClick={() => toast.clear()}>Clear</button>
                </div>
            );
        }

        it('shows toast when triggered', () => {
            render(<ToastProvider><TestConsumer /></ToastProvider>);
            fireEvent.click(screen.getByText('Success'));
            expect(screen.getByText('Done!')).toBeInTheDocument();
        });

        it('shows multiple toasts', () => {
            render(<ToastProvider><TestConsumer /></ToastProvider>);
            fireEvent.click(screen.getByText('Success'));
            fireEvent.click(screen.getByText('Error'));
            expect(screen.getByText('Done!')).toBeInTheDocument();
            expect(screen.getByText('Failed!')).toBeInTheDocument();
        });

        it('clears all toasts', () => {
            render(<ToastProvider><TestConsumer /></ToastProvider>);
            fireEvent.click(screen.getByText('Success'));
            expect(screen.getByText('Done!')).toBeInTheDocument();
            fireEvent.click(screen.getByText('Clear'));
            expect(screen.queryByText('Done!')).not.toBeInTheDocument();
        });

        it('respects maxToasts limit', () => {
            render(<ToastProvider maxToasts={2}><TestConsumer /></ToastProvider>);
            fireEvent.click(screen.getByText('Success'));
            fireEvent.click(screen.getByText('Error'));
            fireEvent.click(screen.getByText('Success'));
            // Should only show 2 most recent
            expect(screen.getAllByRole('alert')).toHaveLength(2);
        });
    });

    describe('useToast outside provider', () => {
        it('returns no-op functions', () => {
            function TestOutside() {
                const toast = useToast();
                return <button onClick={() => toast.show('test')}>Click</button>;
            }
            render(<TestOutside />);
            // Should not throw
            expect(() => fireEvent.click(screen.getByText('Click'))).not.toThrow();
        });
    });
});
