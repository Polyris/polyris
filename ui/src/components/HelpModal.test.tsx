/**
 * HelpModal tests (v0.78.3, ADR #64).
 *
 * Covers:
 *   - Renders all four tabs
 *   - Keyboard shortcut 1/2/3/4 switches tabs in declaration order
 *   - Shortcuts disabled when modal closed
 *   - KeyboardShortcutsTab lists the grouped bindings
 */

import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

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

import { HelpModal, withAuthHeader, AUTH_CURL_HEADER } from './HelpModal';

describe('HelpModal', () => {
    it('renders the tab buttons (Backfill and API are paid, hidden in OSS)', () => {
        render(<HelpModal isOpen onClose={vi.fn()} />);
        expect(screen.getByRole('button', { name: /shortcuts/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /icons/i })).toBeInTheDocument();
        // Backfill and API are paid surfaces — their help tabs are absent in the OSS build.
        expect(screen.queryByRole('button', { name: /backfill/i })).not.toBeInTheDocument();
        expect(screen.queryByRole('button', { name: /api/i })).not.toBeInTheDocument();
    });

    it('lands on Shortcuts tab by default', () => {
        const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
        // The shortcuts tab is active and its content (group titles) is shown
        expect(container.textContent).toMatch(/Global/);
        expect(container.textContent).toMatch(/List views/);
    });

    // v0.78.3+ — keyboard shortcuts (ADR #64; revised v0.78.5).
    // v0.78.5: switched from numeric to letter keys.
    // s=Shortcuts, i=Icons, b=Backfill, a=API.
    describe('keyboard shortcuts', () => {
        it('pressing "i" switches to Icons tab', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            fireEvent.keyDown(document, { key: 'i' });
            const active = container.querySelector('.active');
            expect(active?.textContent).toMatch(/icons/i);
        });

        it('pressing "b" does nothing in OSS (Backfill tab is a paid surface)', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            fireEvent.keyDown(document, { key: 'b' });
            // Backfill help is absent in OSS, so the active tab stays on Shortcuts.
            const active = container.querySelector('.active');
            expect(active?.textContent).toMatch(/shortcuts/i);
        });

        it('pressing "a" does nothing in OSS (API tab is a paid surface)', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            fireEvent.keyDown(document, { key: 'a' });
            // API help is absent in OSS, so the active tab stays on Shortcuts.
            const active = container.querySelector('.active');
            expect(active?.textContent).toMatch(/shortcuts/i);
        });

        it('shortcuts are disabled when modal is closed', () => {
            // Modal closed — pressing "i" should not error and component renders nothing
            const { container } = render(<HelpModal isOpen={false} onClose={vi.fn()} />);
            expect(container.innerHTML).toBe('');
            // No error from pressing key with modal closed
            fireEvent.keyDown(document, { key: 'i' });
        });
    });

    describe('KeyboardShortcutsTab content', () => {
        it('shows grouped bindings by surface type', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            // Group titles per ADR #64 convention
            expect(container.textContent).toMatch(/Global/);
            expect(container.textContent).toMatch(/Top-level navigation/);
            expect(container.textContent).toMatch(/List views/);
            expect(container.textContent).toMatch(/Detail pages/);
            expect(container.textContent).toMatch(/Pipeline view modes/);
            expect(container.textContent).toMatch(/Task Detail modal tabs/);
            expect(container.textContent).toMatch(/Help modal tabs/);
            expect(container.textContent).toMatch(/Modals with primary action/);
            // Asset detail tabs are a paid surface — the whole group is dropped
            // in OSS rather than listing six keys that do nothing (#19).
            expect(container.textContent).not.toMatch(/Asset detail tabs/);
        });

        it('omits the Backfills-list row navigation keys in OSS', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            // j/k/Enter are wired on the paid Backfills list only.
            expect(container.textContent).not.toMatch(/Highlight next row/);
            expect(container.textContent).not.toMatch(/Highlight previous row/);
            expect(container.textContent).not.toMatch(/Open highlighted row/);
        });

        it('mentions ctrl+enter for modal submit', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            // The ⌘↵ glyph is shown for ctrl+enter
            expect(container.textContent).toMatch(/⌘↵|ctrl\+enter/i);
        });

        it('lists only the numeric keys wired in this build', () => {
            const { container } = render(<HelpModal isOpen onClose={vi.fn()} />);
            // 1/3/4 are free; 2 (Assets) and 5 (Backfills) route to paid views
            // that App.tsx no-ops when the slot is empty, so OSS must not
            // advertise them (#19 — the list and the wiring stay in sync).
            expect(container.textContent).toMatch(/Pipelines view/);
            expect(container.textContent).toMatch(/History \(Tasks\)/);
            expect(container.textContent).toMatch(/History \(Runs\)/);
            expect(container.textContent).not.toMatch(/Assets view/);
            expect(container.textContent).not.toMatch(/Backfills view/);
        });
    });
});

describe('withAuthHeader (API docs curl examples require a token)', () => {
    it('injects the Authorization header into a GET curl', () => {
        expect(withAuthHeader('curl "https://x/api/pipelines"')).toBe(
            `curl ${AUTH_CURL_HEADER} "https://x/api/pipelines"`,
        );
    });

    it('injects it after -X METHOD for a POST curl, preserving other flags', () => {
        const out = withAuthHeader('curl -X POST "https://x/api/run" \\\n  -d \'{}\'');
        expect(out.startsWith(`curl -X POST ${AUTH_CURL_HEADER} "https://x/api/run"`)).toBe(true);
        expect(out).toContain("-d '{}'");
    });

    it('references $API_TOKEN so examples are copy-paste runnable after export', () => {
        expect(withAuthHeader('curl https://x/api/pipelines')).toContain('Bearer $API_TOKEN');
    });

    it('is idempotent when an Authorization header is already present', () => {
        const cmd = 'curl -H "Authorization: Bearer abc" "https://x/api/pipelines"';
        expect(withAuthHeader(cmd)).toBe(cmd);
    });

    it('leaves non-curl strings untouched', () => {
        expect(withAuthHeader('echo hello')).toBe('echo hello');
    });
});
