import React, { useState, useEffect, useRef, useMemo } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
    Workflow,
    Package,
    ListTodo,
    Activity,
    Sun,
    Moon,
    RefreshCw,
    HelpCircle,
    Pause,
    XCircle,
    Loader2,
    CheckCircle2,
    Search
} from '@/utils/icons';
import { formatSchedule } from '@/utils/formatters';
import type { PipelineWithUI } from '@/types';

interface CommandItem {
    id?: string;
    type: 'command' | 'pipeline' | 'header' | 'empty';
    icon?: React.ReactNode;
    label: string;
    sublabel?: string;
    shortcut?: string;
    action?: () => void;
}

/**
 * CommandPalette - Spotlight-style command palette (⌘K)
 * 
 * Features:
 * - Fuzzy search across pipelines, assets, and commands
 * - Keyboard navigation (↑↓ to select, Enter to execute, Esc to close)
 * - Grouped results by category
 */
export function CommandPalette({ 
    isOpen, 
    onClose, 
    pipelines = [], 
    onSelectPipeline,
    onNavigate,
    onToggleTheme,
    theme
}: { isOpen: boolean; onClose: () => void; pipelines?: PipelineWithUI[]; onSelectPipeline: (pipeline: PipelineWithUI) => void; onNavigate: (view: string) => void; onToggleTheme: () => void; theme: string }) {
    const [query, setQuery] = useState('');
    const [selectedIndex, setSelectedIndex] = useState(0);
    const inputRef = useRef<HTMLInputElement>(null);
    const listRef = useRef<HTMLDivElement>(null);
    const queryClient = useQueryClient();
    
    // Reset state when opened
    useEffect(() => {
        if (isOpen) {
            // eslint-disable-next-line react-hooks/set-state-in-effect -- reset query when the palette opens/closes
            setQuery('');
            setSelectedIndex(0);
            // Focus input after animation
            setTimeout(() => inputRef.current?.focus(), 50);
        }
    }, [isOpen]);
    
    // Build search results
    const results = useMemo(() => {
        const items: CommandItem[] = [];
        const q = query.toLowerCase().trim();
        
        // Commands (always show first few)
        const commands: CommandItem[] = [
            { id: 'cmd-pipelines', type: 'command', icon: <Workflow size={16} />, label: 'Go to Pipelines', shortcut: '1', action: () => onNavigate('pipelines') },
            { id: 'cmd-assets', type: 'command', icon: <Package size={16} />, label: 'Go to Assets', shortcut: '2', action: () => onNavigate('assets') },
            { id: 'cmd-tasks', type: 'command', icon: <ListTodo size={16} />, label: 'Go to History (Tasks)', shortcut: '3', action: () => onNavigate('tasks') },
            { id: 'cmd-runs', type: 'command', icon: <Activity size={16} />, label: 'Go to History (Runs)', shortcut: '4', action: () => onNavigate('runs') },
            { id: 'cmd-theme', type: 'command', icon: theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />, label: `Switch to ${theme === 'dark' ? 'Light' : 'Dark'} Mode`, shortcut: 'T', action: onToggleTheme },
            { id: 'cmd-refresh', type: 'command', icon: <RefreshCw size={16} />, label: 'Refresh Data', shortcut: 'R', action: () => { queryClient.invalidateQueries(); onClose(); } },
            { id: 'cmd-help', type: 'command', icon: <HelpCircle size={16} />, label: 'Show Keyboard Shortcuts', shortcut: '?', action: () => { onClose(); onNavigate('help'); } },
        ];
        
        // Filter commands
        const filteredCommands = q 
            ? commands.filter(c => c.label.toLowerCase().includes(q))
            : commands.slice(0, 4); // Show first 4 if no query
        
        if (filteredCommands.length > 0) {
            items.push({ type: 'header', label: 'Commands' });
            items.push(...filteredCommands);
        }
        
        // Pipelines
        if (pipelines.length > 0) {
            const filteredPipelines = q
                ? pipelines.filter(p => p.name.toLowerCase().includes(q))
                : pipelines.slice(0, 5);
            
            if (filteredPipelines.length > 0) {
                items.push({ type: 'header', label: 'Pipelines' });
                filteredPipelines.forEach(p => {
                    const statusIcon = p.is_paused ? <Pause size={14} className="text-amber-500" /> : 
                                      (p.today_stats?.failed ?? 0) > 0 ? <XCircle size={14} className="text-red-500" /> : 
                                      (p.today_stats?.running ?? 0) > 0 ? <Loader2 size={14} className="text-blue-500 animate-spin" /> : 
                                      <CheckCircle2 size={14} className="text-green-500" />;
                    items.push({
                        id: `pipeline-${p.name}`,
                        type: 'pipeline',
                        icon: statusIcon,
                        label: p.name,
                        sublabel: formatSchedule(p.schedule, p.asset_schedule),
                        action: () => onSelectPipeline(p)
                    });
                });
            }
        }
        
        // If no results
        if (items.length === 0 && q) {
            items.push({ type: 'empty', label: `No results for "${q}"` });
        }
        
        return items;
    }, [query, pipelines, theme, onNavigate, onSelectPipeline, onToggleTheme, onClose, queryClient]);
    
    // Get selectable items (not headers)
    const selectableItems = results.filter(r => r.type !== 'header' && r.type !== 'empty');
    
    // Keyboard navigation
    useEffect(() => {
        if (!isOpen) return;
        
        const handleKeyDown = (e: KeyboardEvent) => {
            switch (e.key) {
                case 'ArrowDown':
                    e.preventDefault();
                    setSelectedIndex(i => Math.min(i + 1, selectableItems.length - 1));
                    break;
                case 'ArrowUp':
                    e.preventDefault();
                    setSelectedIndex(i => Math.max(i - 1, 0));
                    break;
                case 'Enter': {
                    e.preventDefault();
                    const item = selectableItems[selectedIndex];
                    if (item?.action) {
                        item.action();
                        onClose();
                    }
                    break;
                }
                case 'Escape':
                    e.preventDefault();
                    onClose();
                    break;
            }
        };
        
        document.addEventListener('keydown', handleKeyDown as EventListener);
        return () => document.removeEventListener('keydown', handleKeyDown as EventListener);
    }, [isOpen, selectedIndex, selectableItems, onClose]);
    
    // Scroll selected item into view
    useEffect(() => {
        if (!listRef.current) return;
        const selected = listRef.current.querySelector('.command-item.selected');
        if (selected) {
            selected.scrollIntoView({ block: 'nearest' });
        }
    }, [selectedIndex]);
    
    // Reset selection when results change
    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- reset selection when the result set changes
        setSelectedIndex(0);
    }, [query]);
    
    if (!isOpen) return null;
    
    let selectableIndex = -1;
    
    return (
        <div className="cp-command-palette-overlay" onClick={onClose} role="presentation">
            <div className="cp-command-palette" onClick={e => e.stopPropagation()} role="dialog" aria-label="Command palette">
                <div className="cp-command-palette-input-wrapper">
                    <span className="cp-command-palette-icon"><Search size={16} className="text-muted" /></span>
                    <input
                        ref={inputRef}
                        type="text"
                        className="cp-command-palette-input"
                        placeholder="Search pipelines, commands..."
                        value={query}
                        onChange={e => setQuery(e.target.value)}
                        autoComplete="off"
                        spellCheck="false"
                        role="combobox"
                        aria-expanded="true"
                        aria-controls="command-palette-results"
                        aria-activedescendant={`command-item-${selectedIndex}`}
                        aria-label="Search commands"
                    />
                    <kbd className="cp-command-palette-kbd">ESC</kbd>
                </div>
                
                <div className="cp-command-palette-results" ref={listRef} role="listbox" id="command-palette-results">
                    {results.map((item, _index) => {
                        if (item.type === 'header') {
                            return (
                                <div key={item.label} className="cp-command-palette-header">
                                    {item.label}
                                </div>
                            );
                        }
                        
                        if (item.type === 'empty') {
                            return (
                                <div key="empty" className="cp-command-palette-empty">
                                    {item.label}
                                </div>
                            );
                        }
                        
                        selectableIndex++;
                        const isSelected = selectableIndex === selectedIndex;
                        
                        return (
                            <div
                                key={item.id}
                                id={`command-item-${selectableIndex}`}
                                className={`cp-command-item ${isSelected ? 'selected' : ''}`}
                                role="option"
                                aria-selected={isSelected}
                                onClick={() => {
                                    item.action?.();
                                    onClose();
                                }}
                                onMouseEnter={() => setSelectedIndex(selectableIndex)}
                            >
                                <span className="cp-command-item-icon">{item.icon}</span>
                                <div className="cp-command-item-content">
                                    <span className="cp-command-item-label">{item.label}</span>
                                    {item.sublabel && (
                                        <span className="cp-command-item-sublabel">{item.sublabel}</span>
                                    )}
                                </div>
                                {item.shortcut && <kbd className="cp-command-item-shortcut">{item.shortcut}</kbd>}
                                {isSelected && <span className="cp-command-item-hint">↵</span>}
                            </div>
                        );
                    })}
                </div>
                
                <div className="cp-command-palette-footer">
                    <span><kbd>↑↓</kbd> navigate</span>
                    <span><kbd>↵</kbd> select</span>
                    <span><kbd>esc</kbd> close</span>
                </div>
            </div>
        </div>
    );
}

export default CommandPalette;
