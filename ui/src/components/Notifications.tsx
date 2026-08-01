import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useNotificationsQuery } from '@/hooks/queries';
import { useAuth } from '@/hooks/useAuth';
import { Bell, BellOff, Check, Siren, AlertTriangle, ArrowRight, X, ActionIcons } from '@/utils/icons';
import { MS } from '@/utils/constants';
import { logger } from '@/utils/logger';
import { asStringArray } from '@/utils/formatters';
import type { Notification as NotificationType } from '@/types';

/**
 * Notifications component - shows toast notifications for pipeline failures
 * Features:
 * - Bell icon with badge showing notification count
 * - Dropdown panel to view all notifications
 * - Browser notifications when permission is granted
 * - Toast-style notifications that appear automatically
 * 
 * Data fetching via React Query (useNotificationsQuery) — polls every 30s
 */
function getInitialShownNotifs(): Set<string> {
    try {
        const saved = localStorage.getItem('polyris-shown-browser-notifs');
        if (saved) {
            const { ids, timestamp } = JSON.parse(saved);
            if (Date.now() - timestamp < 4 * MS.HOUR) {
                return new Set(asStringArray(ids));
            }
        }
    } catch {
        // localStorage may be unavailable
    }
    return new Set<string>();
}

interface NotificationsProps {
    onNavigate: (pipelineName: string, execution?: string) => void;
    /** ADR #68 — navigate to backfill detail when a backfill notification is clicked. */
    onNavigateBackfill?: (backfillId: string) => void;
}

interface NotificationsInnerProps extends NotificationsProps {
    /** Scope key for per-user state (signed-in user, or 'anon'). Supplied by the wrapper. */
    userKey: string;
}

// Read notifications linger this long (dimmed) after being acknowledged, then auto-expire.
// Kept in step with the notification query window below so a read item isn't dropped early.
const READ_NOTIFICATION_EXPIRY_MS = 48 * 60 * 60 * 1000;

// Per-user localStorage helpers. Read/dismissed/enabled state is scoped by the signed-in
// user so two Cognito users on the same browser don't share state. (Browser OS-notification
// dedup stays unscoped below — that is correctly per-device, not per-user.)
const loadSet = (key: string): Set<string> => {
    try {
        const s = localStorage.getItem(key);
        return s ? new Set(asStringArray(JSON.parse(s))) : new Set();
    } catch {
        return new Set();
    }
};
const loadMap = (key: string): Record<string, number> => {
    try {
        const s = localStorage.getItem(key);
        return s ? (JSON.parse(s) as Record<string, number>) : {};
    } catch {
        return {};
    }
};
const loadBool = (key: string, dflt: boolean): boolean => {
    try {
        const s = localStorage.getItem(key);
        return s === null ? dflt : s === 'true';
    } catch {
        return dflt;
    }
};

function NotificationsInner({ onNavigate, onNavigateBackfill, userKey }: NotificationsInnerProps) {
    const kDismissed = `polyris-dismissed-notifications:${userKey}`;
    const kRead = `polyris-read-notifications:${userKey}`;
    const kEnabled = `polyris-notifications-enabled:${userKey}`;

    const [dismissed, setDismissed] = useState<Set<string>>(() => loadSet(kDismissed));
    // Acknowledged ("read") notifications: id -> timestamp. A read notification stays
    // visible (dimmed) and auto-expires READ_NOTIFICATION_EXPIRY_MS after being read.
    const [readAt, setReadAt] = useState<Record<string, number>>(() => loadMap(kRead));
    // Bell toggle: when off, no OS popups fire and the bell shows a muted state.
    const [enabled, setEnabled] = useState<boolean>(() => loadBool(kEnabled, true));
    const [now, setNow] = useState(() => Date.now());
    const shownBrowserNotifsRef = useRef<Set<string>>(getInitialShownNotifs());
    const [permissionGranted, setPermissionGranted] = useState(
        () => typeof window !== 'undefined' && 'Notification' in window && Notification.permission === 'granted'
    );
    const [showDropdown, setShowDropdown] = useState(false);
    const dropdownRef = useRef<HTMLDivElement>(null);
    
    // Fetch notifications via React Query (polls every 30s). 48h window so failures from
    // overnight / previous-day runs (dated by schedule) still surface, and read items can
    // linger the full read-expiry without being dropped by the query aging them out.
    const { data: allNotifications = [] } = useNotificationsQuery(10, 48);
    
    // Visible notifications: drop dismissed (Clear All) and read-then-expired ones, and
    // tag each with isRead so acknowledged ones render dimmed while they linger.
    const notifications: (NotificationType & { isRead: boolean })[] = allNotifications
        .filter((n: NotificationType) => !dismissed.has(n.id))
        .filter((n: NotificationType) => {
            const r = readAt[n.id];
            return !r || now - r < READ_NOTIFICATION_EXPIRY_MS;
        })
        .map((n: NotificationType) => ({ ...n, isRead: !!readAt[n.id] }));
    
    // Re-evaluate read-expiry on a timer (independent of the 30s query poll).
    useEffect(() => {
        const t = setInterval(() => setNow(Date.now()), 30 * 1000);
        return () => clearInterval(t);
    }, []);
    
    // Save dismissed to (user-scoped) localStorage
    useEffect(() => {
        try {
            localStorage.setItem(kDismissed, JSON.stringify([...dismissed]));
        } catch {
            // localStorage may be unavailable
        }
    }, [dismissed, kDismissed]);
    
    // Save read map, pruned to the query window so it can't grow unbounded.
    useEffect(() => {
        try {
            const cutoff = Date.now() - 49 * 60 * 60 * 1000;
            const pruned = Object.fromEntries(Object.entries(readAt).filter(([, ts]) => ts > cutoff));
            localStorage.setItem(kRead, JSON.stringify(pruned));
        } catch {
            // localStorage may be unavailable
        }
    }, [readAt, kRead]);
    
    // Save the enabled toggle.
    useEffect(() => {
        try {
            localStorage.setItem(kEnabled, String(enabled));
        } catch {
            // localStorage may be unavailable
        }
    }, [enabled, kEnabled]);
    
    // (shownBrowserNotifs persisted inline when updated)
    
    // Request browser notification permission on mount
    useEffect(() => {
        if ('Notification' in window && Notification.permission !== 'granted' && Notification.permission !== 'denied') {
            Notification.requestPermission().then(permission => {
                setPermissionGranted(permission === 'granted');
            });
        }
    }, []);
    
    // Close dropdown when clicking outside
    useEffect(() => {
        const handleClickOutside = (event: MouseEvent) => {
            if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
                setShowDropdown(false);
            }
        };
        
        document.addEventListener('mousedown', handleClickOutside as EventListener);
        return () => document.removeEventListener('mousedown', handleClickOutside as EventListener);
    }, []);
    
    // Show browser notifications for unseen failures
    useEffect(() => {
        if (!enabled || !permissionGranted || allNotifications.length === 0) return;
        
        const shownBrowserNotifs = shownBrowserNotifsRef.current;
        const unseenNotifs = allNotifications.filter(
            (n: NotificationType) => !shownBrowserNotifs.has(n.id)
        );
        
        if (unseenNotifs.length === 0) return;
        
        unseenNotifs.forEach((n: NotificationType) => {
            try {
                new Notification(
                    n.type === 'decision_required' ? 'Needs Decision' : 'Pipeline Failed',
                    {
                        body: `${n.pipeline_name} • ${n.task_name}`,
                        icon: '/favicon.ico',
                        tag: n.id
                    }
                );
                shownBrowserNotifs.add(n.id);
            } catch (e: unknown) {
                logger.warn('notifications', 'Failed to show browser notification', e);
            }
        });
        
        // Persist to localStorage
        try {
            localStorage.setItem('polyris-shown-browser-notifs', JSON.stringify({
                ids: [...shownBrowserNotifs],
                timestamp: Date.now()
            }));
        } catch {
            // localStorage may be unavailable
        }
    }, [allNotifications, permissionGranted, enabled]);
    
    // Acknowledge ("read") a notification: keep it visible but dimmed; it auto-expires later.
    const markRead = useCallback((id: string | number) => {
        setReadAt(prev => ({ ...prev, [String(id)]: Date.now() }));
    }, []);
    
    // Dismiss all notifications
    const dismissAll = useCallback(() => {
        const allIds = notifications.map(n => n.id);
        setDismissed(prev => new Set([...prev, ...allIds]));
        setShowDropdown(false);
    }, [notifications]);
    
    // Handle "View" click - navigate to the failed pipeline, then mark read (it lingers).
    const handleView = useCallback((notification: NotificationType) => {
        // ADR #68 — backfill notifications navigate to backfill detail.
        if (notification.type === 'backfill' && notification.backfill_id && onNavigateBackfill) {
            onNavigateBackfill(notification.backfill_id);
            markRead(notification.id);
            setShowDropdown(false);
            return;
        }
        if (onNavigate) {
            onNavigate(notification.pipeline_name ?? '', notification.pipeline_execution);
        }
        markRead(notification.id);
        setShowDropdown(false);
    }, [onNavigate, onNavigateBackfill, markRead]);
    
    const notificationCount = notifications.length;
    // Bell badge counts only unread items — read ones linger in the list but don't glow.
    const unreadCount = notifications.filter(n => !n.isRead).length;
    
    return (
        <>
            {/* Bell Icon in Header */}
            <div className="ntf-notification-bell-container" ref={dropdownRef}>
                <button 
                    className={`ntf-notification-bell ${enabled && unreadCount > 0 ? 'has-notifications' : ''}`}
                    onClick={() => setShowDropdown(!showDropdown)}
                    aria-label={!enabled ? 'Notifications off' : unreadCount > 0 ? `${unreadCount} notifications` : 'No notifications'}
                    aria-expanded={showDropdown}
                    aria-haspopup="true"
                    title={!enabled ? 'Notifications off' : unreadCount > 0 ? `${unreadCount} notifications` : 'No notifications'}
                >
                    {enabled ? <Bell size={18} /> : <BellOff size={18} />}
                    {enabled && unreadCount > 0 && (
                        <span className="ntf-notification-badge">{unreadCount > 9 ? '9+' : unreadCount}</span>
                    )}
                </button>
                
                {/* Dropdown Panel */}
                {showDropdown && (
                    <div className="ntf-notification-dropdown">
                        <div className="ntf-notification-dropdown-header">
                            <span className="ntf-notification-dropdown-title">Notifications</span>
                            <div className="ntf-notification-header-actions">
                                <button
                                    className="ntf-notification-toggle"
                                    onClick={() => setEnabled(e => !e)}
                                    title={enabled ? 'Turn notifications off' : 'Turn notifications on'}
                                    aria-label={enabled ? 'Turn notifications off' : 'Turn notifications on'}
                                    aria-pressed={enabled}
                                >
                                    {enabled ? <Bell size={14} /> : <BellOff size={14} />}
                                </button>
                                {notificationCount > 0 && (
                                    <button 
                                        className="ntf-notification-clear-all"
                                        onClick={dismissAll}
                                    >
                                        Clear All
                                    </button>
                                )}
                            </div>
                        </div>
                        <div className="ntf-notification-dropdown-content">
                            {notifications.length === 0 ? (
                                <div className="ntf-notification-empty">
                                    <span className="ntf-notification-empty-icon">
                                        <Check size={18} className="text-green-500" />
                                    </span>
                                    <span>No new notifications</span>
                                </div>
                            ) : (
                                notifications.map(notification => (
                                    <div 
                                        key={notification.id} 
                                        className={`ntf-notification-item ${notification.type}${notification.isRead ? ' ntf-notification-item--read' : ''}`}
                                    >
                                        <div className="ntf-notification-item-icon">
                                            {notification.type === 'backfill'
                                                ? <ActionIcons.backfill size={16} className={
                                                    notification.backfill_status === 'completed' ? 'text-green-500'
                                                    : notification.backfill_status === 'failed' ? 'text-red-500'
                                                    : notification.backfill_status === 'partial' ? 'text-amber-500'
                                                    : 'text-slate-400'
                                                } />
                                                : notification.type === 'decision_required'
                                                ? <AlertTriangle size={16} className="text-orange-500" />
                                                : notification.type === 'failure'
                                                ? <Siren size={16} className="text-red-500" />
                                                : <AlertTriangle size={16} className="text-amber-500" />
                                            }
                                        </div>
                                        <div className="ntf-notification-item-content">
                                            <div className="ntf-notification-item-title">
                                                {notification.type === 'backfill'
                                                    ? `Backfill ${notification.backfill_status}: ${notification.target_pipeline}`
                                                    : notification.pipeline_name}
                                            </div>
                                            <div className="ntf-notification-item-subtitle">
                                                {notification.type === 'backfill'
                                                    ? `${notification.completed_partitions}/${notification.total_partitions} partitions • ${notification.time_ago}`
                                                    : notification.type === 'decision_required'
                                                    ? `${notification.task_name} • awaiting decision • ${notification.time_ago}`
                                                    : `${notification.task_name} • ${notification.time_ago}`}
                                            </div>
                                        </div>
                                        <div className="ntf-notification-item-actions">
                                            <button 
                                                className="ntf-notification-item-btn"
                                                onClick={() => handleView(notification)}
                                                title="View"
                                                aria-label={
                                                    notification.type === 'backfill'
                                                        ? `View backfill ${notification.backfill_id}`
                                                        : notification.type === 'decision_required'
                                                        ? `View ${notification.pipeline_name} decision`
                                                        : `View ${notification.pipeline_name} failure`
                                                }
                                            >
                                                <ArrowRight size={14} />
                                            </button>
                                            <button 
                                                className="ntf-notification-item-btn ntf-dismiss"
                                                onClick={() => markRead(notification.id)}
                                                title="Mark as read"
                                                aria-label="Mark notification as read"
                                            >
                                                <X size={14} />
                                            </button>
                                        </div>
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                )}
            </div>
            
            {/* Toast Notifications (floating) — only unread, and only while enabled */}
            {enabled && unreadCount > 0 && !showDropdown && (
                <div className="ntf-notifications-container" role="log" aria-live="polite" aria-label="Pipeline failure notifications">
                    {notifications.filter(n => !n.isRead).slice(0, 3).map(notification => (
                        <div 
                            key={notification.id} 
                            className={`ntf-notification-toast ${notification.type}`}
                        >
                            <span className="ntf-notification-icon">
                                {notification.type === 'backfill'
                                    ? <ActionIcons.backfill size={18} className={
                                        notification.backfill_status === 'completed' ? 'text-green-500'
                                        : notification.backfill_status === 'failed' ? 'text-red-500'
                                        : notification.backfill_status === 'partial' ? 'text-amber-500'
                                        : 'text-slate-400'
                                    } />
                                    : notification.type === 'decision_required'
                                    ? <AlertTriangle size={18} className="text-orange-500" />
                                    : notification.type === 'failure'
                                    ? <Siren size={18} className="text-red-500" />
                                    : <AlertTriangle size={18} className="text-amber-500" />
                                }
                            </span>
                            <div className="ntf-notification-content">
                                <div className="ntf-notification-title">
                                    {notification.type === 'backfill'
                                        ? `Backfill ${notification.backfill_status}`
                                        : notification.type === 'decision_required'
                                        ? 'Needs Decision'
                                        : 'Pipeline Failed'}
                                </div>
                                <div className="ntf-notification-subtitle">
                                    {notification.type === 'backfill'
                                        ? `${notification.target_pipeline} • ${notification.completed_partitions}/${notification.total_partitions} • ${notification.time_ago}`
                                        : `${notification.pipeline_name} • ${notification.time_ago}`}
                                </div>
                                <div className="ntf-notification-actions">
                                    <button 
                                        className="ntf-notification-btn primary"
                                        onClick={() => handleView(notification)}
                                    >
                                        View
                                    </button>
                                    <button 
                                        className="ntf-notification-btn secondary"
                                        onClick={() => markRead(notification.id)}
                                    >
                                        Mark read
                                    </button>
                                </div>
                            </div>
                            <button 
                                className="ntf-notification-close"
                                onClick={() => markRead(notification.id)}
                                title="Mark as read"
                                aria-label="Mark notification as read"
                            >
                                <X size={14} />
                            </button>
                        </div>
                    ))}
                </div>
            )}
        </>
    );
}

// Keying NotificationsInner by the signed-in user resets all per-user state cleanly on a
// user change (React's recommended pattern for identity-based reset) — the per-user keys
// are read fresh in the child's initializers on remount, so no reload effect is needed.
function Notifications(props: NotificationsProps) {
    const { user } = useAuth();
    const userKey = user?.username || 'anon';
    return <NotificationsInner key={userKey} userKey={userKey} {...props} />;
}

export default Notifications;
