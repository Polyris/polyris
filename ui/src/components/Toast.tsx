'use client';

import React, { useEffect, useState, useCallback, createContext, useContext } from 'react';
import { ToastIcons, X } from '@/utils/icons';

interface ToastItemProps {
    id: number | string;
    message: string;
    type?: string;
    onClose: () => void;
    duration?: number;
}

interface ToastData {
    id: number;
    message: string;
    type: string;
    duration: number;
}

interface ToastApi {
    show: (message: string, type?: string, duration?: number) => number | void;
    success: (msg: string, duration?: number) => number | void;
    error: (msg: string, duration?: number) => number | void;
    warning: (msg: string, duration?: number) => number | void;
    info: (msg: string, duration?: number) => number | void;
    clear: () => void;
}

/**
 * Toast - Single notification component
 */
function ToastItem({ id: _id, message, type = 'info', onClose, duration = 4000 }: ToastItemProps) {
    const [isExiting, setIsExiting] = useState(false);
    
    useEffect(() => {
        const exitTimer = setTimeout(() => setIsExiting(true), duration - 300);
        const closeTimer = setTimeout(onClose, duration);
        return () => {
            clearTimeout(exitTimer);
            clearTimeout(closeTimer);
        };
    }, [onClose, duration]);

    const handleClose = () => {
        setIsExiting(true);
        setTimeout(onClose, 300);
    };

    const IconComponent = ToastIcons[type as keyof typeof ToastIcons] || ToastIcons.info;

    return (
        <div className={`tst-toast toast-${type} ${isExiting ? 'toast-exit' : 'toast-enter'}`} role="alert">
            <span className="tst-toast-icon">
                <IconComponent size={18} />
            </span>
            <span className="tst-toast-message">{message}</span>
            <button className="tst-toast-close" onClick={handleClose} aria-label="Close"><X size={14} /></button>
        </div>
    );
}

/**
 * Toast Container - Manages multiple toasts with queue
 */
export function ToastContainer({ toasts, onRemove }: { toasts: ToastData[]; onRemove: (id: number) => void }) {
    if (!toasts || toasts.length === 0) return null;
    
    return (
        <div className="tst-toast-container" role="status" aria-live="polite" aria-label="Notifications">
            {toasts.map((toast: ToastData) => (
                <ToastItem
                    key={toast.id}
                    {...toast}
                    onClose={() => onRemove(toast.id)}
                />
            ))}
        </div>
    );
}

/**
 * Toast Context for global access
 */
const ToastContext = createContext<ToastApi | null>(null);

export function ToastProvider({ children, maxToasts = 5 }: { children: React.ReactNode; maxToasts?: number }) {
    const [toasts, setToasts] = useState<ToastData[]>([]);
    
    const addToast = useCallback((message: string, type = 'info', duration = 4000) => {
        const id = Date.now() + Math.random();
        setToasts(prev => {
            const newToasts = [...prev, { id, message, type, duration }];
            return newToasts.slice(-maxToasts);
        });
        return id;
    }, [maxToasts]);
    
    const removeToast = useCallback((id: number) => {
        setToasts(prev => prev.filter(t => t.id !== id));
    }, []);
    
    const clearToasts = useCallback(() => {
        setToasts([]);
    }, []);
    
    const toast: ToastApi = {
        show: addToast,
        success: (msg: string, duration?: number) => addToast(msg, 'success', duration),
        error: (msg: string, duration?: number) => addToast(msg, 'error', duration),
        warning: (msg: string, duration?: number) => addToast(msg, 'warning', duration),
        info: (msg: string, duration?: number) => addToast(msg, 'info', duration),
        clear: clearToasts,
    };
    
    return (
        <ToastContext.Provider value={toast}>
            {children}
            <ToastContainer toasts={toasts} onRemove={removeToast} />
        </ToastContext.Provider>
    );
}

/**
 * Hook for using toast
 */
export function useToast(): ToastApi {
    const context = useContext(ToastContext);
    if (!context) {
        return {
            show: () => {},
            success: () => {},
            error: () => {},
            warning: () => {},
            info: () => {},
            clear: () => {},
        };
    }
    return context;
}

/**
 * Legacy Toast component for backward compatibility
 */
export function Toast({ message, type = 'info', onClose, duration = 4000 }: { message: string; type?: string; onClose: () => void; duration?: number }) {
    return (
        <div className="tst-toast-container">
            <ToastItem 
                id="legacy" 
                message={message} 
                type={type} 
                onClose={onClose} 
                duration={duration} 
            />
        </div>
    );
}

export default Toast;
