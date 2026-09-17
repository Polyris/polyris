import React, { useState, useMemo } from 'react';
import { Button } from '@/components/ui/button';
import {
    Database,
    AlertTriangle,
    ChevronDown,
    ChevronRight,
    Copy,
    CheckCircle2,
    Wrench,
} from '../../utils/icons';
import { CollapsibleJsonBlock } from './CollapsibleJsonBlock';
import {
    detectManualResolution,
    formatManualResolution,
    statusForResolution,
    variantForResolution,
} from './manualResolution';
import { TASK_SETTLED_STATUSES } from '@/generated/enums';

interface Props {
    output: unknown;
    /** True when the wrapper couldn't inline the payload (>25 KB legacy cap). */
    truncated?: boolean;
    /** True when the raw output looks like an AWS API response (JobRunId etc). */
    awsMetadata?: boolean;
    /**
     * The task's current per-run status. Used to gate whether the canonical
     * `output#{pipeline}#{task}#{date}` row is authoritative for THIS run:
     * that row is date-scoped, so multiple same-date runs of the same task
     * share it. A stale row from a prior same-date run (e.g. manual skip)
     * shows here whenever the current run's task hasn't reached a settled
     * state — we render an empty state in that case rather than lying about
     * whose output it is. Optional for backwards compatibility.
     */
    taskStatus?: string;
}

/**
 * Renders task output as a single card in the same visual language as
 * upstream deps — icon + label + status badge + click-to-expand. Collapsed
 * shows a one-line compact JSON preview; expanded shows pretty JSON with
 * copy affordances.
 *
 * Variants: normal / empty / truncated / aws-metadata — each carries a
 * distinct icon + status badge so the user reads intent at a glance.
 */
export const OutputCard: React.FC<Props> = ({ output, truncated, awsMetadata, taskStatus }) => {
    const [expanded, setExpanded] = useState(false);
    const [copied, setCopied] = useState(false);

    const compactJson = useMemo(
        () => (output === undefined || output === null ? '' : JSON.stringify(output)),
        [output],
    );

    // Gate the canonical-row read on the task's settled state. Rows keyed by
    // (pipeline, task, date) are shared across all same-date runs — a task
    // still in `waiting_decision` / `running` / etc. cannot own the row's
    // content until Save_Success (or a manual action) writes it for THIS
    // run. Rendering the row before that leaks a prior run's data.
    // (See CLAUDE.md rule about date-scoped canonical DDB rows.)
    const isSettled = taskStatus === undefined || TASK_SETTLED_STATUSES.includes(taskStatus);

    const toggle = () => setExpanded(!expanded);
    const onHeaderKey = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggle();
        }
    };

    // Truncated — wrapper couldn't inline the payload. No output body to render;
    // just a warn card explaining the S3 escape hatch. Non-expandable.
    if (truncated) {
        return (
            <div className="td-upstream-dep td-upstream-dep--warn td-output-card">
                <div className="td-output-card-header">
                    <AlertTriangle size={14} />
                    <strong>Output</strong>
                    <span className="td-status-badge td-status-badge--warn">truncated</span>
                </div>
                <div className="td-output-card-message">
                    Output too large to store inline. Write the payload to S3 and return{' '}
                    <code>{'{"_s3_ref": "s3://..."}'}</code> — downstream{' '}
                    <code>xcom.get()</code> / <code>xcom.pull()</code> resolves it automatically.
                </div>
            </div>
        );
    }

    // Empty — task stored nothing. Muted card, non-expandable.
    if (output === null || output === undefined) {
        return (
            <div className="td-upstream-dep td-upstream-dep--muted td-output-card">
                <div className="td-output-card-header">
                    <Database size={14} />
                    <strong>Output</strong>
                    <span className="td-status-badge td-status-badge--muted">empty</span>
                </div>
                <div className="td-output-card-message">This task stored no output.</div>
            </div>
        );
    }

    // Non-settled task: any content in the canonical row belongs to a prior
    // same-date run, not this one. Render as pending rather than lie about
    // whose output it is. Copy button still exposes the raw JSON on demand
    // — advanced users diagnosing multi-run state don't lose access.
    if (!isSettled) {
        return (
            <div className="td-upstream-dep td-upstream-dep--muted td-output-card">
                <div className="td-output-card-header">
                    <Database size={14} />
                    <strong>Output</strong>
                    <span className="td-status-badge td-status-badge--muted">pending</span>
                </div>
                <div className="td-output-card-message">
                    This run hasn&apos;t reached a settled state yet
                    {taskStatus ? <> (current status: <code>{taskStatus}</code>)</> : null}
                    . Output will appear once the task completes.
                </div>
            </div>
        );
    }

    const handleCopy = (e: React.MouseEvent) => {
        e.stopPropagation();
        navigator.clipboard.writeText(JSON.stringify(output, null, 2));
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
    };

    // Manual resolution — the task's own output is a synthetic marker written
    // by console_api (mark_success / skip / fail / stop via UI). Render as the
    // shared blue "manual" variant so a downstream operator opening this
    // task's Output tab sees "Marked X by <operator>" instead of raw JSON.
    const manual = detectManualResolution(output);

    let badgeLabel: string;
    let badgeClass: string;
    let cardVariant: string;
    let HeaderIcon: React.ComponentType<{ size?: number }>;

    if (manual) {
        // Primary badge mirrors what UpstreamDep shows for the same task —
        // derived from the resolution the operator applied (mark_success →
        // 'success', skip → 'skipped', fail → 'failed', stop → 'stopped').
        // Colour tracks intent (mark_success stays green; skip/fail red;
        // stop muted) so the badge doesn't lie about outcome.
        badgeLabel = statusForResolution(manual.resolution);
        badgeClass = `td-status-badge--${variantForResolution(manual.resolution)}`;
        cardVariant = 'td-upstream-dep--manual';
        HeaderIcon = Wrench;
    } else if (awsMetadata) {
        badgeLabel = 'aws-metadata';
        badgeClass = 'td-status-badge--warn';
        cardVariant = 'td-upstream-dep--warn';
        HeaderIcon = AlertTriangle;
    } else {
        badgeLabel = 'success';
        badgeClass = 'td-status-badge--success';
        cardVariant = 'td-upstream-dep--success';
        HeaderIcon = Database;
    }

    return (
        <div className={`td-upstream-dep td-output-card ${cardVariant} ${expanded ? 'expanded' : ''}`}>
            <div
                role="button"
                tabIndex={0}
                className="td-output-card-header td-output-card-header--clickable"
                onClick={toggle}
                onKeyDown={onHeaderKey}
                aria-expanded={expanded}
                aria-label={expanded ? 'Collapse output' : 'Expand output'}
            >
                <HeaderIcon size={14} />
                <strong>Output</strong>
                {manual && (
                    <span className="td-status-badge td-status-badge--manual">
                        manual: {manual.resolution}
                    </span>
                )}
                <span className={`td-status-badge ${badgeClass}`}>{badgeLabel}</span>
                <div className="td-output-card-actions">
                    {copied && <span className="td-copy-feedback">Copied!</span>}
                    <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6 opacity-60 hover:opacity-100"
                        onClick={handleCopy}
                        title="Copy output"
                        aria-label={copied ? 'Copied' : 'Copy output'}
                    >
                        {copied ? <CheckCircle2 size={14} /> : <Copy size={14} />}
                    </Button>
                    {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </div>
            </div>
            {awsMetadata && expanded && (
                <div className="td-banner td-banner--warn">
                    <AlertTriangle size={14} />
                    <div>
                        This output is an AWS API response, not application data. For
                        Glue/ECS/Batch tasks, call <code>xcom.push(value)</code> in your job
                        code so downstream tasks receive the real output.
                    </div>
                </div>
            )}
            {expanded ? (
                <CollapsibleJsonBlock
                    value={output}
                    ariaLabel={manual ? 'Task output raw marker' : 'Task output'}
                />
            ) : (
                <pre className="td-output-preview" aria-label="Task output preview">
                    {manual ? formatManualResolution(manual) : compactJson}
                </pre>
            )}
        </div>
    );
};
