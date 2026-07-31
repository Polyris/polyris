/**
 * UserMenu Component
 * 
 * Dropdown menu for authenticated users showing:
 * - User info (email)
 * - Sign out action
 * - Admin-only: User management link
 * 
 * @module components/UserMenu
 */

import React, { useState, useRef, useEffect, useCallback } from 'react';
import type { UserMenuProps } from '@/types';
import { useAuth } from '@/hooks/useAuth';
import { useAppStore } from '@/stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { 
    LogOut, 
    ChevronUp,
    Users,
    Settings,
    HelpCircle,
    Moon,
    Loader2
} from 'lucide-react';
import { SettingsModal } from './SettingsModal';

/**
 * Get user initials for avatar
 */
const getInitials = (name: string, email: string) => {
    if (name && name !== email) {
        return name
            .split(' ')
            .map((part: string) => part[0])
            .join('')
            .toUpperCase()
            .slice(0, 2);
    }
    return email?.charAt(0).toUpperCase() || 'U';
};

/**
 * UserMenu - User dropdown in header
 */
export function UserMenu({ onManageUsers }: UserMenuProps) {
    const { user, signOut, isAuthEnabled } = useAuth();
    const { setShowHelpModal, theme, toggleTheme } = useAppStore(useShallow(s => ({
        setShowHelpModal: s.setShowHelpModal, theme: s.theme, toggleTheme: s.toggleTheme,
    })));
    const [isOpen, setIsOpen] = useState(false);
    const [isSigningOut, setIsSigningOut] = useState(false);
    const [settingsOpen, setSettingsOpen] = useState(false);
    const menuRef = useRef<HTMLDivElement>(null);
    
    // Close menu when clicking outside
    useEffect(() => {
        const handleClickOutside = (e: Event) => {
            if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
                setIsOpen(false);
            }
        };
        
        if (isOpen) {
            document.addEventListener('mousedown', handleClickOutside);
        }
        
        return () => {
            document.removeEventListener('mousedown', handleClickOutside);
        };
    }, [isOpen]);
    
    // Close on escape
    useEffect(() => {
        const handleEscape = (e: Event) => {
            if ((e as KeyboardEvent).key === 'Escape') {
                setIsOpen(false);
            }
        };
        
        if (isOpen) {
            document.addEventListener('keydown', handleEscape);
        }
        
        return () => {
            document.removeEventListener('keydown', handleEscape);
        };
    }, [isOpen]);
    
    const handleSignOut = useCallback(async () => {
        setIsSigningOut(true);
        try {
            await signOut();
        } finally {
            setIsSigningOut(false);
            setIsOpen(false);
        }
    }, [signOut]);
    
    // When auth is enabled but nobody is signed in, the login screen shows instead.
    if (isAuthEnabled && !user) {
        return null;
    }

    // Authenticated when auth is on and a user is present; otherwise render a minimal
    // app menu (Settings + Help) so both stay reachable in no-auth deployments.
    const authed = isAuthEnabled && !!user;
    const initials = authed ? getInitials(user!.name || '', user!.email || '') : '';
    const displayName = authed ? (user!.name || user!.email?.split('@')[0] || 'User') : '';
    
    return (
        <div className="um-user-menu um-user-menu--rail" ref={menuRef}>
            <button
                className="um-user-menu-trigger"
                onClick={() => setIsOpen(!isOpen)}
                aria-expanded={isOpen}
                aria-haspopup="true"
                aria-label={authed ? `Account: ${displayName}` : 'Menu'}
            >
                {authed ? (
                    <>
                        <div className="um-user-avatar">{initials}</div>
                        <span className="um-user-name">{displayName}</span>
                    </>
                ) : (
                    <Settings size={18} />
                )}
                <ChevronUp size={14} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
            </button>
            
            {isOpen && (
                <div className="um-user-menu-dropdown" role="menu">
                    {authed && (
                        <div className="um-user-menu-header">
                            <div className="um-user-avatar um-user-avatar--lg">{initials}</div>
                            <div className="um-user-menu-identity">
                                <div className="um-user-menu-name">{displayName}</div>
                                <div className="um-user-menu-email">{user!.email}</div>
                            </div>
                        </div>
                    )}

                    <div className="um-user-menu-content">
                        <button
                            className="um-user-menu-item um-user-menu-toggle"
                            onClick={toggleTheme}
                            role="menuitemcheckbox"
                            aria-checked={theme === 'dark'}
                        >
                            <span className="um-toggle-label"><Moon size={16} /> Dark mode</span>
                            <span className={`um-toggle-switch ${theme === 'dark' ? 'is-on' : ''}`} aria-hidden="true"><span className="um-toggle-knob" /></span>
                        </button>
                        <button
                            className="um-user-menu-item"
                            onClick={() => {
                                setIsOpen(false);
                                setSettingsOpen(true);
                            }}
                            role="menuitem"
                        >
                            <Settings size={16} />
                            Settings
                        </button>
                        <button
                            className="um-user-menu-item"
                            onClick={() => {
                                setIsOpen(false);
                                setShowHelpModal(true);
                            }}
                            role="menuitem"
                        >
                            <HelpCircle size={16} />
                            Help &amp; documentation
                        </button>

                        {authed && (
                            <>
                                <div className="um-user-menu-divider" />

                                {user!.isAdmin && onManageUsers && (
                                    <>
                                        <button
                                            className="um-user-menu-item"
                                            onClick={() => {
                                                setIsOpen(false);
                                                onManageUsers();
                                            }}
                                            role="menuitem"
                                        >
                                            <Users size={16} />
                                            Manage Users
                                        </button>
                                        <div className="um-user-menu-divider" />
                                    </>
                                )}

                                <button
                                    className="um-user-menu-item um-destructive"
                                    onClick={handleSignOut}
                                    disabled={isSigningOut}
                                    role="menuitem"
                                >
                                    {isSigningOut ? (
                                        <>
                                            <Loader2 size={16} className="animate-spin" />
                                            Signing out...
                                        </>
                                    ) : (
                                        <>
                                            <LogOut size={16} />
                                            Sign Out
                                        </>
                                    )}
                                </button>
                            </>
                        )}
                    </div>
                </div>
            )}

            <SettingsModal isOpen={settingsOpen} onClose={() => setSettingsOpen(false)} />
        </div>
    );
}


export default UserMenu;
