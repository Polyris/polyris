import React, { useEffect, useRef, useCallback, useId, createContext, useContext } from 'react';
import type { ModalHeaderProps, ModalBodyProps, ModalFooterProps } from '@/types';
import { X } from '@/utils/icons';

const FOCUSABLE_SELECTOR = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

/** Context to pass the title id from BaseModal to ModalHeader */
const ModalTitleIdContext = createContext<string | undefined>(undefined);

/**
 * BaseModal - Base modal component with common functionality
 * 
 * Features:
 * - Click outside to close
 * - ESC key to close  
 * - Focus trap (Tab/Shift+Tab cycle within modal)
 * - Returns focus to trigger element on close
 * - Prevents body scroll when open
 * - ARIA: role=dialog, aria-modal, aria-labelledby
 * 
 * Usage:
 *   <BaseModal isOpen={isOpen} onClose={onClose} className="bm-my-modal">
 *     <ModalHeader onClose={onClose}>Title</ModalHeader>
 *     <ModalBody>...</ModalBody>
 *     <ModalFooter>...</ModalFooter>
 *   </BaseModal>
 */
export function BaseModal({ 
    isOpen, 
    onClose, 
    children, 
    className = 'modal',
    closeOnEsc = true,
    closeOnOverlay = true,
}: { isOpen: boolean; onClose?: () => void; children: React.ReactNode; className?: string; closeOnEsc?: boolean; closeOnOverlay?: boolean }) {
    const modalRef = useRef<HTMLDivElement>(null);
    const previousFocusRef = useRef<HTMLElement | null>(null);
    const titleId = useId();
    
    // Save the element that triggered the modal
    useEffect(() => {
        if (isOpen) {
            previousFocusRef.current = document.activeElement as HTMLElement;
        }
    }, [isOpen]);
    
    // Handle ESC key
    useEffect(() => {
        if (!isOpen || !closeOnEsc) return;
        
        const handleEsc = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose?.();
        };
        
        document.addEventListener('keydown', handleEsc);
        return () => document.removeEventListener('keydown', handleEsc);
    }, [isOpen, onClose, closeOnEsc]);
    
    // Focus trap: Tab/Shift+Tab cycle within modal
    useEffect(() => {
        if (!isOpen || !modalRef.current) return;
        
        const handleTab = (e: KeyboardEvent) => {
            if (e.key !== 'Tab' || !modalRef.current) return;
            
            const focusable = modalRef.current.querySelectorAll(FOCUSABLE_SELECTOR);
            if (focusable.length === 0) return;
            
            const first = focusable[0] as HTMLElement;
            const last = focusable[focusable.length - 1] as HTMLElement;
            
            if (e.shiftKey) {
                if (document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                }
            } else {
                if (document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };
        
        document.addEventListener('keydown', handleTab);
        return () => document.removeEventListener('keydown', handleTab);
    }, [isOpen]);
    
    // Focus first focusable element when modal opens
    useEffect(() => {
        if (!isOpen || !modalRef.current) return;
        
        const focusableElements = modalRef.current.querySelectorAll(FOCUSABLE_SELECTOR);
        
        if (focusableElements.length > 0) {
            setTimeout(() => (focusableElements[0] as HTMLElement).focus(), 50);
        }
    }, [isOpen]);
    
    // Return focus to trigger element on close
    useEffect(() => {
        if (!isOpen && previousFocusRef.current) {
            previousFocusRef.current.focus();
            previousFocusRef.current = null;
        }
    }, [isOpen]);
    
    // Prevent body scroll when modal is open
    useEffect(() => {
        if (isOpen) {
            const originalOverflow = document.body.style.overflow;
            document.body.style.overflow = 'hidden';
            return () => {
                document.body.style.overflow = originalOverflow;
            };
        }
    }, [isOpen]);
    
    const handleOverlayClick = useCallback((e: React.MouseEvent) => {
        if (closeOnOverlay && e.target === e.currentTarget) {
            onClose?.();
        }
    }, [closeOnOverlay, onClose]);
    
    if (!isOpen) return null;
    
    return (
        <div className="bm-modal-overlay" onClick={handleOverlayClick} role="presentation">
            <div 
                ref={modalRef}
                className={className} 
                onClick={e => e.stopPropagation()}
                role="dialog"
                aria-modal="true"
                aria-labelledby={titleId}
            >
                <ModalTitleIdContext.Provider value={titleId}>
                    {children}
                </ModalTitleIdContext.Provider>
            </div>
        </div>
    );
}

/**
 * ModalHeader - Standard modal header with close button
 */
export function ModalHeader({ children, onClose, icon }: ModalHeaderProps) {
    const titleId = useContext(ModalTitleIdContext);
    return (
        <div className="bm-modal-header">
            {icon && <span className="bm-modal-icon">{icon}</span>}
            <h3 className="bm-modal-title" id={titleId}>{children}</h3>
            {onClose && (
                <button className="modal-close" onClick={onClose} aria-label="Close dialog">
                    <X size={18} />
                </button>
            )}
        </div>
    );
}

/**
 * ModalBody - Standard modal body
 */
export function ModalBody({ children, className = '' }: ModalBodyProps) {
    return (
        <div className={`modal-body ${className}`.trim()}>
            {children}
        </div>
    );
}

/**
 * ModalFooter - Standard modal footer
 */
export function ModalFooter({ children, className = '' }: ModalFooterProps) {
    return (
        <div className={`modal-footer ${className}`.trim()}>
            {children}
        </div>
    );
}
