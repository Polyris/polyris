import { useEffect, useCallback, useState } from 'react';

/**
 * useKeyboardShortcuts - Global keyboard shortcuts hook
 * 
 * Usage:
 *   const shortcuts = useKeyboardShortcuts({
 *       'ctrl+k': () => openSearch(),
 *       '?': () => openHelp(),
 *       'escape': () => closeModal(),
 *   });
 *   
 *   // In your component
 *   <HelpButton onClick={shortcuts.showHelp} />
 */

// Parse shortcut string like "ctrl+shift+k" into parts
function parseShortcut(shortcut: string) {
    const parts = shortcut.toLowerCase().split('+');
    return {
        ctrl: parts.includes('ctrl') || parts.includes('cmd') || parts.includes('meta'),
        shift: parts.includes('shift'),
        alt: parts.includes('alt'),
        key: parts.filter((p: string) => !['ctrl', 'cmd', 'meta', 'shift', 'alt'].includes(p))[0] || ''
    };
}

// Check if event matches shortcut
function matchesShortcut(event: KeyboardEvent, shortcut: string) {
    const parsed = parseShortcut(shortcut);
    const eventKey = event.key.toLowerCase();
    
    // Handle special keys
    const keyMatches = 
        eventKey === parsed.key || 
        event.code.toLowerCase() === `key${parsed.key}` ||
        (parsed.key === 'escape' && eventKey === 'escape') ||
        (parsed.key === '/' && eventKey === '/') ||
        (parsed.key === '?' && event.shiftKey && eventKey === '/');
    
    const modifiersMatch = 
        (event.ctrlKey || event.metaKey) === parsed.ctrl &&
        event.shiftKey === (parsed.shift || parsed.key === '?') &&
        event.altKey === parsed.alt;
    
    return keyMatches && modifiersMatch;
}

export function useKeyboardShortcuts(shortcuts: Record<string, (event?: KeyboardEvent) => void>, options: { enabled?: boolean; ignoreInputs?: boolean; preventDefault?: boolean } = {}) {
    const { 
        enabled = true, 
        ignoreInputs = true,  // Ignore when typing in input/textarea
        preventDefault = true 
    } = options;
    
    const [showShortcutsModal, setShowShortcutsModal] = useState(false);
    
    const handleKeyDown = useCallback((event: KeyboardEvent) => {
        if (!enabled) return;
        
        // Ignore when typing in inputs (unless it's Escape)
        if (ignoreInputs && event.key !== 'Escape') {
            const target = event.target as HTMLElement;
            if (
                target.tagName === 'INPUT' || 
                target.tagName === 'TEXTAREA' || 
                target.isContentEditable
            ) {
                return;
            }
        }
        
        for (const [shortcut, handler] of Object.entries(shortcuts)) {
            if (matchesShortcut(event, shortcut)) {
                if (preventDefault) {
                    event.preventDefault();
                }
                handler(event);
                break;
            }
        }
    }, [shortcuts, enabled, ignoreInputs, preventDefault]);
    
    useEffect(() => {
        document.addEventListener('keydown', handleKeyDown);
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [handleKeyDown]);
    
    return {
        showShortcutsModal,
        setShowShortcutsModal,
    };
}

/**
 * Predefined shortcut sets for common use cases.
 *
 * Convention (v0.78.3, ADR #64) — every new surface MUST wire the
 * shortcuts appropriate to its type. See CLAUDE.md #19.
 */
export const SHORTCUTS = {
    // ─── Navigation ───────────────────────────────────────────────────────
    SEARCH: 'ctrl+k',
    HELP: '?',
    ESCAPE: 'escape',
    FOCUS_FILTER: '/',             // Focus the primary search/filter input
    OPEN_SELECTED: 'enter',        // Open the highlighted row (list views)

    // ─── Actions ──────────────────────────────────────────────────────────
    REFRESH: 'ctrl+r',
    NEW: 'ctrl+n',
    SAVE: 'ctrl+s',
    SUBMIT: 'ctrl+enter',          // Submit primary modal action

    // ─── Views / Layout ───────────────────────────────────────────────────
    TOGGLE_SIDEBAR: 'ctrl+b',
    TOGGLE_THEME: 'ctrl+shift+t',

    // ─── List row navigation (vim-style) ──────────────────────────────────
    NEXT_TASK: 'j',
    PREV_TASK: 'k',
    EXPAND: 'e',
    COLLAPSE: 'c',

    // ─── Pipeline-specific ────────────────────────────────────────────────
    TRIGGER: 'ctrl+enter',
};

// Note: TAB_1..TAB_9 constants were removed in v0.78.5 — numeric keys 1-9
// are reserved for top-level navigation in App.tsx (1=Pipelines, 2=Assets,
// 3=Tasks, 4=Runs, 5=Backfills). Inner-surface tab switching uses letter
// keys matching the first letter of the tab name. See ADR #64 for the
// revised convention and the conflict that motivated the change.

/**
 * KeyboardShortcutsHelp - Component to display available shortcuts
 */
export function KeyboardShortcutsHelp({ shortcuts }: { shortcuts: Record<string, string> }) {
    const formatShortcut = (shortcut: string) => {
        return shortcut
            .replace('ctrl', '⌘')
            .replace('shift', '⇧')
            .replace('alt', '⌥')
            .replace('escape', 'Esc')
            .replace('+', ' + ')
            .toUpperCase();
    };
    
    return (
        <div className="keyboard-shortcuts-help">
            <h3>Keyboard Shortcuts</h3>
            <div className="shortcuts-grid">
                {Object.entries(shortcuts).map(([action, shortcut]) => (
                    <div key={action} className="shortcut-item">
                        <kbd className="shortcut-key">{formatShortcut(shortcut)}</kbd>
                        <span className="shortcut-action">{action.replace(/_/g, ' ')}</span>
                    </div>
                ))}
            </div>
        </div>
    );
}

export default useKeyboardShortcuts;
