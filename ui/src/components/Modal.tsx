import React, { useRef, useEffect } from 'react';
import type { ModalProps } from '@/types';
import { Button } from '@/components/ui/button';
import { BaseModal, ModalHeader, ModalBody, ModalFooter } from './BaseModal';

/**
 * Modal - Simple modal for alerts and confirmations
 * Now uses BaseModal for consistent behavior (ESC, click outside, focus trap)
 */
export function Modal({ 
    isOpen, 
    onClose, 
    onConfirm, 
    title, 
    message, 
    icon, 
    confirmText = 'OK', 
    confirmVariant = 'default', 
    loading = false,
    children 
}: ModalProps) {
    const confirmRef = useRef<HTMLButtonElement>(null);
    
    // Auto-focus confirm button when modal opens
    useEffect(() => {
        if (isOpen) {
            setTimeout(() => confirmRef.current?.focus(), 100);
        }
    }, [isOpen]);
    
    const showCancel = onConfirm && confirmText !== 'OK';
    
    return (
        <BaseModal isOpen={isOpen} onClose={loading ? undefined : onClose} className="modal">
            <ModalHeader icon={icon} onClose={loading ? undefined : onClose}>{title}</ModalHeader>
            <ModalBody>
                {message && <p className="m-0">{message}</p>}
                {children}
            </ModalBody>
            <ModalFooter>
                {showCancel && (
                    <Button variant="secondary" onClick={onClose} disabled={loading}>
                        Cancel
                    </Button>
                )}
                <Button 
                    ref={confirmRef}
                    variant={confirmVariant} 
                    onClick={onConfirm || onClose}
                    disabled={loading}
                >
                    {loading ? 'Working…' : confirmText}
                </Button>
            </ModalFooter>
        </BaseModal>
    );
}

export default Modal;
