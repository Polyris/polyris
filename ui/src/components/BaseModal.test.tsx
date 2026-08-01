import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { BaseModal, ModalHeader, ModalBody, ModalFooter } from './BaseModal';

vi.mock('@/utils/icons', () => ({
    X: () => <span data-testid="icon-x">×</span>,
}));

describe('BaseModal', () => {
    describe('rendering', () => {
        it('renders nothing when closed', () => {
            const { container } = render(
                <BaseModal isOpen={false} onClose={vi.fn()}>
                    <div>Content</div>
                </BaseModal>
            );
            expect(container.innerHTML).toBe('');
        });

        it('renders children when open', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <div>Modal Content</div>
                </BaseModal>
            );
            expect(screen.getByText('Modal Content')).toBeInTheDocument();
        });

        it('has correct ARIA attributes', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <ModalHeader>Test Title</ModalHeader>
                    <ModalBody>Body</ModalBody>
                </BaseModal>
            );
            const dialog = screen.getByRole('dialog');
            expect(dialog).toHaveAttribute('aria-modal', 'true');
            expect(dialog).toHaveAttribute('aria-labelledby');
        });
    });

    describe('interactions', () => {
        it('calls onClose on ESC key', () => {
            const onClose = vi.fn();
            render(
                <BaseModal isOpen={true} onClose={onClose}>
                    <div>Content</div>
                </BaseModal>
            );
            fireEvent.keyDown(document, { key: 'Escape' });
            expect(onClose).toHaveBeenCalledTimes(1);
        });

        it('calls onClose on overlay click', () => {
            const onClose = vi.fn();
            render(
                <BaseModal isOpen={true} onClose={onClose}>
                    <div>Content</div>
                </BaseModal>
            );
            const overlay = screen.getByRole('presentation');
            fireEvent.click(overlay);
            expect(onClose).toHaveBeenCalledTimes(1);
        });

        it('does not close when clicking inside modal', () => {
            const onClose = vi.fn();
            render(
                <BaseModal isOpen={true} onClose={onClose}>
                    <div>Content</div>
                </BaseModal>
            );
            fireEvent.click(screen.getByText('Content'));
            expect(onClose).not.toHaveBeenCalled();
        });

        it('does not call onClose on ESC when onClose is undefined', () => {
            // Should not throw
            render(
                <BaseModal isOpen={true}>
                    <div>Content</div>
                </BaseModal>
            );
            expect(() => fireEvent.keyDown(document, { key: 'Escape' })).not.toThrow();
        });
    });

    describe('ModalHeader', () => {
        it('renders title text', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <ModalHeader>My Title</ModalHeader>
                </BaseModal>
            );
            expect(screen.getByText('My Title')).toBeInTheDocument();
        });

        it('shows close button with aria-label', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <ModalHeader onClose={vi.fn()}>Title</ModalHeader>
                </BaseModal>
            );
            expect(screen.getByLabelText('Close dialog')).toBeInTheDocument();
        });

        it('renders icon when provided', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <ModalHeader icon={<span data-testid="custom-icon" />}>Title</ModalHeader>
                </BaseModal>
            );
            expect(screen.getByTestId('custom-icon')).toBeInTheDocument();
        });
    });

    describe('ModalBody', () => {
        it('renders children', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <ModalBody><p>Body content</p></ModalBody>
                </BaseModal>
            );
            expect(screen.getByText('Body content')).toBeInTheDocument();
        });
    });

    describe('ModalFooter', () => {
        it('renders children', () => {
            render(
                <BaseModal isOpen={true} onClose={vi.fn()}>
                    <ModalFooter><button>OK</button></ModalFooter>
                </BaseModal>
            );
            expect(screen.getByText('OK')).toBeInTheDocument();
        });
    });
});
