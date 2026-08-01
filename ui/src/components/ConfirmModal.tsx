import React, { useRef, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { BaseModal, ModalHeader, ModalBody, ModalFooter } from './BaseModal';
import { AlertTriangle, CircleHelp } from '@/utils/icons';
import type { ConfirmModalProps } from '@/types';

/**
 * ConfirmModal - Unified confirmation dialog
 * 
 * Usage:
 *   const [confirm, setConfirm] = useState({ isOpen: false });
 *   const showConfirm = (config) => setConfirm({ isOpen: true, ...config });
 *   const closeConfirm = () => setConfirm({ isOpen: false });
 *   
 *   <ConfirmModal
 *       isOpen={confirm.isOpen}
 *       title="Delete Item"
 *       message="Are you sure?"
 *       confirmText="Delete"
 *       danger={true}
 *       onConfirm={() => { doSomething(); closeConfirm(); }}
 *       onCancel={closeConfirm}
 *   />
 */
export function ConfirmModal({ 
    isOpen, 
    title, 
    message, 
    onConfirm, 
    onCancel, 
    confirmText = 'Confirm', 
    cancelText = 'Cancel',
    danger = false,
    icon = null
}: ConfirmModalProps) {
    const confirmButtonRef = useRef<HTMLButtonElement>(null);
    
    // Focus confirm button when modal opens
    useEffect(() => {
        if (isOpen) {
            setTimeout(() => confirmButtonRef.current?.focus(), 100);
        }
    }, [isOpen]);

    const defaultIcon = danger ? <AlertTriangle size={18} /> : <CircleHelp size={18} />;

    return (
        <BaseModal isOpen={isOpen} onClose={onCancel} className="modal">
            <ModalHeader icon={icon || defaultIcon}>{title}</ModalHeader>
            <ModalBody>
                <p className="whitespace-pre-line m-0">{message}</p>
            </ModalBody>
            <ModalFooter>
                <Button variant="secondary" onClick={onCancel}>
                    {cancelText}
                </Button>
                <Button 
                    ref={confirmButtonRef}
                    variant={danger ? 'destructive' : 'default'}
                    onClick={onConfirm ?? undefined}
                >
                    {confirmText}
                </Button>
            </ModalFooter>
        </BaseModal>
    );
}

export default ConfirmModal;
