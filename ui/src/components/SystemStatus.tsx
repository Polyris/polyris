import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQueryClient } from '@tanstack/react-query';
import { useSystemStatusQuery, SYSTEM_STATUS_POLL_MINUTES } from '@/hooks/queries';
import { queryKeys } from '@/lib/queryClient';
import type { ConnectionProblem } from '@/utils/api';
import type { SystemFinding, SystemStatusState } from '@/hooks/queries';
import { useKeyboardShortcuts, SHORTCUTS } from '@/hooks/useKeyboardShortcuts';
import { useIsMobileWidth } from '@/hooks/useIsMobileWidth';
import { useToast } from '@/components/Toast';
import { BaseModal, ModalHeader } from '@/components/BaseModal';
import { asStringArray, formatAgo, formatDateTime, onClickOutside } from '@/utils/formatters';
import { AlertTriangle, Check, CheckCircle2, ChevronDown, Circle, CircleDashed, Copy, X } from '@/utils/icons';

type Kind = SystemStatusState['kind'];

const PILL: Record<Kind, { suffix: string; Icon: typeof Circle }> = {
    loading: { suffix: '', Icon: Circle },
    unknown: { suffix: 'status unknown', Icon: CircleDashed },
    connection: { suffix: 'no connection', Icon: AlertTriangle },
    attention: { suffix: 'needs attention', Icon: AlertTriangle },
    healthy: { suffix: '', Icon: CheckCircle2 },
};

const HEALTHY_ROWS = [
    'The console API reaches DynamoDB and Step Functions',
    'Asset event delivery is on',
    'The periodic re-check is running (every 5 minutes)',
    'Waiting tasks are re-checked on time',
];

const AFTER_A_FIX = 'After a fix, the next check clears it within 5 minutes; reopen this panel to see it.';
const NEEDS_SIGN_IN = 'The details need a verified sign-in. Sign in again; if that does not help, the console API logs say why.';

/** The pill's word: its level's, except connection, which says whether the API is gone or erring. */
const suffixOf = (status: SystemStatusState) => (status.kind === 'connection' ? status.pill : PILL[status.kind].suffix);
/** The visible label; the accessible name adds ", all checks pass" when healthy. */
const pillLabel = (status: SystemStatusState) => (suffixOf(status) ? `System · ${suffixOf(status)}` : 'System');
const accessibleName = (status: SystemStatusState) => (status.kind === 'healthy' ? 'System, all checks pass' : pillLabel(status));

/** The line under the panel's title; null while there is no answer to describe. */
function metaLine(status: SystemStatusState): string | null {
    if (status.kind !== 'healthy' && status.kind !== 'attention') return null;
    let head = 'All checks pass';
    if (status.kind === 'attention') {
        const count = status.findings.length || status.unhealthy.length;
        head = `${count} problem${count === 1 ? '' : 's'}`;
    }
    const ago = formatAgo(status.checkedAt);
    return ago ? `${head} · checked ${ago}` : head;
}

// ─── Toast: one per new finding title per browser session ────────────────────

const TOASTED_KEY = 'polyris-system-status-toasted';

function readToasted(): string[] {
    try {
        const saved = sessionStorage.getItem(TOASTED_KEY);
        return saved ? asStringArray(JSON.parse(saved)) : [];
    } catch {
        // sessionStorage may be unavailable or hold bad JSON; the in-memory set still keeps one toast per title.
        return [];
    }
}

function writeToasted(titles: Set<string>): void {
    try {
        sessionStorage.setItem(TOASTED_KEY, JSON.stringify([...titles]));
    } catch {
        // sessionStorage may be unavailable; the in-memory set still keeps one toast per title.
    }
}

// ─── Panel ───────────────────────────────────────────────────────────────────

function FindingCard({ finding }: { finding: SystemFinding }) {
    const [copied, setCopied] = useState<'yes' | 'failed' | null>(null);
    const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
    useEffect(() => () => clearTimeout(timer.current), []);

    const show = (outcome: 'yes' | 'failed') => {
        setCopied(outcome);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(null), 1500);
    };
    const copy = (command: string) => {
        // The Clipboard API exists only in a secure context: say so rather than claim a copy.
        if (!navigator.clipboard) {
            show('failed');
            return;
        }
        navigator.clipboard.writeText(command).then(() => show('yes'), () => show('failed'));
    };

    const { title, resource, changed_at: changedAt, impact, fix, command } = finding;
    // formatDateTime names the day too: a bare HH:MM reads as today for a change days ago.
    const changedClock = changedAt ? formatDateTime(changedAt) : null;
    const changedAgo = formatAgo(changedAt);
    return (
        <li className="sys-finding">
            <p className="sys-finding__title">
                <AlertTriangle size={16} aria-hidden="true" />
                <strong>{title}</strong>
            </p>
            {resource && <p className="sys-finding__note">{resource}</p>}
            {changedClock && changedAgo && <p className="sys-finding__note">Last changed {changedClock} ({changedAgo})</p>}
            {(impact || fix) && (
                <dl className="sys-finding__kv">
                    {impact && <><dt>Impact</dt><dd>{impact}</dd></>}
                    {fix && <><dt>Fix</dt><dd>{fix}</dd></>}
                </dl>
            )}
            {command && (
                <div className="sys-finding__cmd">
                    <pre className="sys-finding__code"><code>{command}</code></pre>
                    <div className="sys-finding__copy">
                        <button
                            type="button"
                            className="sys-finding__copy-btn"
                            aria-label={`Copy command: ${title}`}
                            onClick={() => copy(command)}
                        >
                            <Copy size={14} aria-hidden="true" />
                            Copy
                        </button>
                        {/* Always rendered, so the text takes reserved space and is announced when it changes. */}
                        <span className="sys-finding__copied" role="status" data-failed={copied === 'failed' || undefined}>
                            {copied === 'yes' ? 'Copied!' : copied === 'failed' ? 'Copy failed' : ''}
                        </span>
                    </div>
                </div>
            )}
        </li>
    );
}

function PanelBody({ status, retrying, onRetry }: { status: SystemStatusState; retrying: boolean; onRetry: () => void }) {
    switch (status.kind) {
        case 'loading':
            return <p className="sys-panel__text">Loading…</p>;
        case 'unknown':
            return (
                <div className="sys-panel__unknown">
                    <p>The status could not be read: {status.reason}. The console retries every {SYSTEM_STATUS_POLL_MINUTES} minutes.</p>
                    <button type="button" className="sys-panel__retry" onClick={onRetry}>
                        {retrying ? 'Checking…' : 'Retry now'}
                    </button>
                </div>
            );
        case 'connection':
            return (
                <div className="sys-panel__unknown" role="alert">
                    <p>{status.reason}. The console shows what it last read and retries on its own.</p>
                    <p className="sys-panel__detail">{status.detail}</p>
                    <button type="button" className="sys-panel__retry" onClick={onRetry}>
                        {retrying ? 'Checking…' : 'Retry now'}
                    </button>
                </div>
            );
        case 'healthy':
            return (
                <ul className="sys-panel__rows sys-panel__rows--ok">
                    {HEALTHY_ROWS.map(row => (
                        <li key={row}><Check size={16} aria-hidden="true" /><span>{row}</span></li>
                    ))}
                </ul>
            );
        case 'attention': {
            // The pipelines read failing too is said first: the console shows what it last read.
            const connection = status.connection && (
                <p className="sys-panel__connection" role="alert">
                    The console also cannot read its pipelines: {status.connection}
                </p>
            );
            // A full answer always names its problems; should it name none, the checks still say what is wrong.
            if (status.detail === 'full' && status.findings.length > 0) {
                return (
                    <>
                        {connection}
                        <ul className="sys-findings">
                            {status.findings.map((finding, index) => (
                                <FindingCard key={`${index}:${finding.title}`} finding={finding} />
                            ))}
                        </ul>
                        <p className="sys-panel__footer">{AFTER_A_FIX}</p>
                    </>
                );
            }
            return (
                <>
                    {connection}
                    <ul className="sys-panel__rows sys-panel__rows--attention">
                        {status.unhealthy.map(label => (
                            <li key={label}><AlertTriangle size={16} aria-hidden="true" /><span>{label}</span></li>
                        ))}
                    </ul>
                    <p className="sys-panel__footer">
                        {status.detail === 'minimal' ? NEEDS_SIGN_IN : AFTER_A_FIX}
                    </p>
                </>
            );
        }
    }
}

/**
 * SystemStatus — the System pill in the header and its panel: whether the console reaches
 * DynamoDB and Step Functions, and whether task coordination works (GET /api/health).
 *
 * The panel opens on click and re-reads the status. Above the mobile breakpoint it is a
 * popover that closes on Esc, on a mousedown outside and when focus leaves it; below it, a
 * modal. After Esc or ×, focus returns to the pill. Each new finding raises one toast per
 * browser session; the minimal answer (no valid token) raises none.
 *
 * It is also the one place that says the pipelines read fails (`connectionError`): amber
 * "no connection" or "API error", the reason in the panel, whose Retry re-reads the pipelines
 * as well. An answer that already needs attention keeps its level and says it there.
 */
export function SystemStatus({ connectionError }: { connectionError: ConnectionProblem | null }) {
    const { status, refetch, isFetching } = useSystemStatusQuery(connectionError);
    const queryClient = useQueryClient();
    const toast = useToast();
    // ToastProvider builds a new toast object with each toast it shows. Read the latest from a ref,
    // so that raising a toast does not re-run the effect that raised it.
    const toastRef = useRef(toast);
    useEffect(() => {
        toastRef.current = toast;
    });
    const [open, setOpen] = useState(false);
    // Below the mobile breakpoint the panel is a modal.
    const isMobile = useIsMobileWidth();

    const rootRef = useRef<HTMLDivElement>(null);
    const pillRef = useRef<HTMLButtonElement>(null);
    const closeRef = useRef<HTMLButtonElement>(null);
    const returnFocus = useRef(false);
    const toasted = useRef<Set<string>>(new Set());
    const panelId = useId();
    const titleId = useId();

    const close = useCallback((toPill: boolean) => {
        returnFocus.current = toPill;
        setOpen(false);
    }, []);

    const toggle = () => {
        if (open) {
            close(false);
            return;
        }
        setOpen(true);
        void refetch();
    };

    const retry = () => {
        if (!isFetching) void refetch();
        // A lost connection is the pipelines read failing: retry that read too.
        if (connectionError !== null) void queryClient.invalidateQueries({ queryKey: queryKeys.pipelines });
    };

    // Focus: into the popover when it opens (the modal moves it itself); back to the pill after Esc or ×.
    useEffect(() => {
        if (open) {
            if (!isMobile) closeRef.current?.focus();
            return;
        }
        if (returnFocus.current) {
            returnFocus.current = false;
            pillRef.current?.focus();
        }
    }, [open, isMobile]);

    // The popover closes on a mousedown outside; the modal closes on its own overlay.
    useEffect(() => {
        if (!open || isMobile) return;
        const outside = onClickOutside(rootRef, () => close(false));
        outside.add();
        return outside.remove;
    }, [open, isMobile, close]);

    useKeyboardShortcuts({ [SHORTCUTS.ESCAPE]: () => close(true) }, { enabled: open });

    const handleBlur = (event: React.FocusEvent<HTMLDivElement>) => {
        if (!open || isMobile) return;
        // No next target: focus left the window (to paste a copied command, say) or a click landed
        // on nothing focusable, which the mousedown check handles. Either way, stay open.
        const next = event.relatedTarget;
        if (next instanceof Node && !event.currentTarget.contains(next)) close(false);
    };

    useEffect(() => {
        if (status.kind !== 'attention' || status.detail !== 'full') return;
        const seen = new Set([...toasted.current, ...readToasted()]);
        let raised = false;
        for (const { title } of status.findings) {
            if (seen.has(title)) continue;
            seen.add(title);
            raised = true;
            toastRef.current.warning(`System needs attention: ${title}. Open System in the header for details.`);
        }
        toasted.current = seen;
        if (raised) writeToasted(seen);
    }, [status]);

    // The live region speaks on a change between answers; the first answer after loading is not news.
    const [spoken, setSpoken] = useState<{ kind: Kind; text: string }>({ kind: status.kind, text: '' });
    if (spoken.kind !== status.kind) {
        setSpoken({ kind: status.kind, text: spoken.kind === 'loading' ? '' : accessibleName(status) });
    }

    const { Icon } = PILL[status.kind];
    const suffix = suffixOf(status);
    const meta = metaLine(status);
    const body = <PanelBody status={status} retrying={isFetching} onRetry={retry} />;

    return (
        <div className="sys-status" ref={rootRef} onBlur={handleBlur}>
            <button
                type="button"
                ref={pillRef}
                className="sys-pill"
                data-level={status.kind}
                aria-label={accessibleName(status)}
                aria-haspopup="dialog"
                aria-expanded={open}
                aria-controls={open && !isMobile ? panelId : undefined}
                onClick={toggle}
            >
                <Icon size={16} className="sys-pill__icon" aria-hidden="true" />
                <span className="sys-pill__label">
                    System{suffix && <span className="sys-pill__suffix"> · {suffix}</span>}
                </span>
                <ChevronDown size={14} className="sys-pill__chevron" aria-hidden="true" />
            </button>
            <span className="sr-only" role="status" aria-live="polite">{spoken.text}</span>

            {open && !isMobile && (
                <div className="sys-popover" id={panelId} role="dialog" aria-labelledby={titleId}>
                    <div className="sys-panel__head">
                        <div>
                            <h2 className="sys-panel__title" id={titleId}>System status</h2>
                            {meta && <p className="sys-panel__meta">{meta}</p>}
                        </div>
                        <button
                            type="button"
                            ref={closeRef}
                            className="sys-panel__close"
                            aria-label="Close"
                            onClick={() => close(true)}
                        >
                            <X size={18} aria-hidden="true" />
                        </button>
                    </div>
                    <div className="sys-panel__body">{body}</div>
                </div>
            )}

            {/* `open` is false on the server, so document is only touched in the browser. */}
            {open && isMobile && createPortal(
                <BaseModal isOpen onClose={() => close(true)} className="modal sys-modal" closeOnEsc={false}>
                    <ModalHeader onClose={() => close(true)}>System status</ModalHeader>
                    {meta && <p className="sys-panel__meta sys-modal__meta">{meta}</p>}
                    <div className="sys-panel__body">{body}</div>
                </BaseModal>,
                document.body,
            )}
        </div>
    );
}

export default SystemStatus;
