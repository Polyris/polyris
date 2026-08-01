/**
 * Open-core shortcut guard (CLAUDE.md #19 + #23, ADR #99).
 *
 * Two failure modes this pins:
 *
 * 1. The Shortcuts tab advertising keys that are not wired in this build. It is
 *    the one Help tab always visible in OSS, and it listed ~14 keys for the
 *    Backfills/Assets surfaces that do not exist there. Gantt/Calendar had been
 *    filtered on `paidSurface`; the rest of the same pattern had not.
 *
 * 2. A paid view-mode key bound at a surface that cannot render it. App.tsx bound
 *    'g'/'c' ungated on top of PipelineDetail's gated bindings, so in OSS 'c' set
 *    viewMode='calendar' — persisted to localStorage and the URL, with the
 *    view-mode pills hidden, hence unreachable — which suppressed PipelineDetail's
 *    "No executions for {date}" branch and left a bare graph.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// ─── Icons stub — HelpModal uses many icons; stub each as null component.
//     Avoid Proxy here because it can cause vitest module resolution to hang.
vi.mock('@/utils/icons', () => ({
    BookOpen: () => null,
    Keyboard: () => null,
    Palette: () => null,
    ContextIcons: new Proxy({}, { get: () => () => null }),
    Plug: () => null,
    Wrench: () => null,
    Package: () => null,
    Download: () => null,
    Rocket: () => null,
    CheckCircle2: () => null,
    AlertTriangle: () => null,
    Clock: () => null,
    Zap: () => null,
    RefreshCw: () => null,
    ClipboardList: () => null,
    Settings: () => null,
    Activity: () => null,
    Bell: () => null,
    XCircle: () => null,
    Loader2: () => null,
    SkipForward: () => null,
    PlayCircle: () => null,
    Ban: () => null,
    StopCircle: () => null,
    Pause: () => null,
    Copy: () => null,
    ChevronDown: () => null,
    ChevronRight: () => null,
}));

// BaseModal stub — same approach as TaskDetailModal.test.tsx.
vi.mock('./BaseModal', () => ({
    BaseModal: ({ isOpen, children, className }: any) =>
        isOpen ? <div data-testid="base-modal" className={className} role="dialog">{children}</div> : null,
    ModalHeader: ({ children, icon, onClose }: any) =>
        <div data-testid="modal-header">{icon}<span>{children}</span>{onClose && <button data-testid="modal-close" onClick={onClose}>x</button>}</div>,
    ModalBody: ({ children }: any) => <div data-testid="modal-body">{children}</div>,
    ModalFooter: ({ children }: any) => <div data-testid="modal-footer">{children}</div>,
}));

vi.mock('@/components/ui/button', () => ({
    Button: (props: any) => <button onClick={props.onClick} disabled={props.disabled}>{props.children}</button>,
}));


// OSS build: src/ee absent -> empty surface (matches the ee-active.generated stub).
vi.mock('@/ee-active.generated', () => ({ paidSurface: {} }));

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { HelpModal } from './HelpModal';

describe('OSS build: Help modal advertises only wired shortcuts', () => {
    const open = () => render(<HelpModal isOpen onClose={() => {}} />);

    // Tab-button gating + per-key behaviour live in HelpModal.test.tsx; this
    // file pins the advertised *list* against the wiring, exhaustively.
    it.each([
        ['Assets view'],
        ['Backfills view'],
        ['Highlight next row (Backfills list)'],
        ['Highlight previous row (Backfills list)'],
        ['Open highlighted row (Backfills list)'],
        ['Switch to Gantt view'],
        ['Switch to Calendar view'],
        ['Backfill tab'],
        ['API tab'],
    ])('does not advertise %s', (label) => {
        open();
        expect(screen.queryByText(label)).toBeNull();
    });

    it('drops the Asset detail tabs group entirely', () => {
        open();
        expect(screen.queryByText('Asset detail tabs')).toBeNull();
        expect(screen.queryByText('Lineage tab')).toBeNull();
    });

    it('narrows the detail-pages group title to the surfaces that exist', () => {
        open();
        expect(screen.getByText('Detail pages (Pipeline)')).toBeInTheDocument();
    });

    it('still advertises the free shortcuts', () => {
        open();
        expect(screen.getByText('Pipelines view')).toBeInTheDocument();
        expect(screen.getByText('History (Runs)')).toBeInTheDocument();
        expect(screen.getByText('Switch to DAG view')).toBeInTheDocument();
    });
});

describe('App.tsx does not bind pipeline view-mode keys', () => {
    // Source-level guard: PipelineDetail owns d/g/c and gates g/c on the paid
    // surface. Re-binding them at App level re-opens the OSS viewMode trap.
    const appSrc = readFileSync(join(__dirname, "..", 'App.tsx'), 'utf8');
    const shortcutMap = appSrc.slice(
        appSrc.indexOf('useKeyboardShortcuts({'),
        appSrc.indexOf('// ========== Render ==========')
    );

    it.each([['g'], ['c'], ['d']])("has no top-level '%s' handler", (key) => {
        expect(shortcutMap).not.toMatch(new RegExp(`^\\s*'${key}'\\s*:`, 'm'));
    });

    it('does not call setViewMode at all', () => {
        expect(appSrc).not.toContain('setViewMode');
    });
});
