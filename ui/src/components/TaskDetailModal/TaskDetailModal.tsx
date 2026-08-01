import React, { useState, useMemo } from 'react';

import { Button } from '@/components/ui/button';
import { TASK_SETTLED_STATUSES } from '@/generated/enums';
import { formatDate, buildAwsConsoleUrl, getUpstreamCount, getDownstreamCount } from '../../utils';
import { formatFreshnessWindow, formatFreshnessWindowLong } from '../../utils/formatters';
import { logger } from '../../utils/logger';
import { useKeyboardShortcuts } from '../../hooks';
import { useTaskOutput } from '../../hooks/useTaskOutput';
import { 
    StatusIcon, 
    CheckCircle2, 
    XCircle, 
    Clock, 
    Pause,
    StopCircle,
    Play, 
    SkipForward, 
    RotateCcw,
    Target,
    CircleDot,
    Copy,
    ExternalLink,
    FileText,
    Zap,
    X,
    Info,
    Check,
    XIcon,
    User,
    Database,
    Hourglass,
    AlertTriangle,
    Calendar,
    Rocket,
} from '../../utils/icons';
import { CountdownTimer } from '../CountdownTimer';
import { BaseModal } from '../BaseModal';
import { LiveDuration } from './LiveDuration';
import { ErrorDisplay } from './ErrorDisplay';
import { useAppStore } from '../../stores/useAppStore';
import type { Task, TaskDetailModalProps } from '@/types';
import { paidSurface } from '@/ee-active.generated';

/**
 * TaskDetailModal - Modal window for viewing task details
 * Split into sub-components: ConsecutiveProgress, LiveDuration, DependencyStatusList, ErrorDisplay
 */

export function TaskDetailModal({ 
    task,
    tasks,
    dag,
    pipelines,
    taskEvents,
    taskEventsLoading,
    onClose,
    onAction,
    onRunAction,
    onTaskSelect,
    onOpenPipeline,
    onPauseResume,
    serverOffsetMs = 0,
 }: TaskDetailModalProps) {
    const [activeTab, setActiveTab] = useState('details');

    // Tab switching per ADR #64 (revised v0.78.5). Numeric keys are
    // reserved for global nav (App.tsx); modals use letters matching
    // first letter of tab content. Enabled only when modal is open.
    useKeyboardShortcuts({
        'd': () => setActiveTab('details'),
        't': () => setActiveTab('timeline'),
        'a': () => setActiveTab('actions'),
        'o': () => setActiveTab('output'),
    }, { enabled: !!task });

    // Fetch the task's stored output only while the Output tab is open (lazy).
    const taskOutput = useTaskOutput(task, activeTab === 'output');
    
    // Calculate upstream/downstream counts using shared utilities
    // NOTE: Must be called before early return to satisfy Rules of Hooks
    const upstreamCount = useMemo(() => task ? getUpstreamCount(task.task_name, dag) : 0, [task, dag]);
    const downstreamCount = useMemo(() => task ? getDownstreamCount(task.task_name, dag) : 0, [task, dag]);
    
    // Detect if this task runs a child pipeline (sfn task whose ARN matches a registered pipeline)
    const childPipeline = useMemo(() => {
        if (!task?.task_arn || !pipelines?.length) return null;
        return pipelines.find(p => p.arn === task.task_arn);
    }, [task, pipelines]);
    
    if (!task) return null;
    
    return (
        <BaseModal isOpen={true} onClose={onClose} className="td-task-modal">
            {/* Header */}
            <div className="td-task-modal-header">
                <div className="td-task-modal-title">
                    <div className={`td-task-icon task-icon-${task.status || 'waiting'}`}>
                        <StatusIcon status={task.status} size={20} />
                    </div>
                    <div>
                        <div className="td-task-title">{task.task_name}</div>
                        <span className={`task-status-badge ${task.status || 'waiting'}`} aria-label={`Task status: ${task.status || 'waiting'}`}>
                            {task.status || 'waiting'}
                        </span>
                    </div>
                </div>
                <button className="modal-close" onClick={onClose} aria-label="Close task details"><X size={18} /></button>
            </div>
            
            {/* Notification failure warning */}
            {task.notification_failed && (
                <div className="td-notification-warning" role="alert">
                    <AlertTriangle size={14} />
                    <span>Notification delivery failed — use the buttons below or the console API to skip, fail, or restart this task</span>
                </div>
            )}
            
            {/* Tabs */}
            <div className="nav-tabs" role="tablist" aria-label="Task detail sections">
                <div 
                    className={`nav-tab nav-tab--lg ${activeTab === 'details' ? 'active' : ''}`} 
                    onClick={() => setActiveTab('details')}
                    role="tab"
                    aria-selected={activeTab === 'details'}
                    tabIndex={activeTab === 'details' ? 0 : -1}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTab('details'); } }}
                >
                    <FileText size={14} /> Details
                </div>
                <div 
                    className={`nav-tab nav-tab--lg ${activeTab === 'timeline' ? 'active' : ''}`} 
                    onClick={() => setActiveTab('timeline')}
                    role="tab"
                    aria-selected={activeTab === 'timeline'}
                    tabIndex={activeTab === 'timeline' ? 0 : -1}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTab('timeline'); } }}
                >
                    <Clock size={14} /> History
                </div>
                <div 
                    className={`nav-tab nav-tab--lg ${activeTab === 'output' ? 'active' : ''}`} 
                    onClick={() => setActiveTab('output')}
                    role="tab"
                    aria-selected={activeTab === 'output'}
                    tabIndex={activeTab === 'output' ? 0 : -1}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTab('output'); } }}
                >
                    <Database size={14} /> Input / Output
                </div>
                {onAction && (
                <div 
                    className={`nav-tab nav-tab--lg ${activeTab === 'actions' ? 'active' : ''}`} 
                    onClick={() => setActiveTab('actions')}
                    role="tab"
                    aria-selected={activeTab === 'actions'}
                    tabIndex={activeTab === 'actions' ? 0 : -1}
                    onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setActiveTab('actions'); } }}
                >
                    <Zap size={14} /> Actions
                </div>
                )}
            </div>
            
            {/* Content */}
            <div className="td-task-modal-content">
                {activeTab === 'details' ? (
                    <DetailsTab
                        task={task}
                        tasks={tasks}
                        dag={dag}
                        childPipeline={childPipeline}
                        serverOffsetMs={serverOffsetMs}
                        onTaskSelect={onTaskSelect}
                        onOpenPipeline={onOpenPipeline}
                        onPauseResume={onPauseResume}
                    />
                ) : activeTab === 'timeline' ? (
                    <TimelineTab
                        task={task}
                        taskEvents={taskEvents}
                        taskEventsLoading={taskEventsLoading}
                    />
                ) : activeTab === 'output' ? (
                    <OutputTab
                        input={taskOutput.input}
                        output={taskOutput.output}
                        truncated={taskOutput.truncated}
                        loading={taskOutput.loading}
                        loaded={taskOutput.loaded}
                    />
                ) : onAction ? (
                    <ActionsTab
                        task={task}
                        upstreamCount={upstreamCount}
                        downstreamCount={downstreamCount}
                        onAction={onAction}
                        onRunAction={onRunAction}
                        onClose={onClose}
                    />
                ) : null}
            </div>
                
                {/* Footer */}
                <div className="td-task-modal-footer">
                    <div className="td-task-modal-hint">
                        <Info size={14} /> Click a task in DAG to open details
                    </div>
                    <Button variant="secondary" onClick={onClose}>Close</Button>
                </div>
        </BaseModal>
    );
}

// =============================================================================
// Tab Components
// =============================================================================

interface DetailsTabProps {
    task: Task;
    tasks: Task[];
    dag: TaskDetailModalProps['dag'];
    childPipeline: { name: string } | null | undefined;
    serverOffsetMs: number;
    onTaskSelect: TaskDetailModalProps['onTaskSelect'];
    onOpenPipeline: TaskDetailModalProps['onOpenPipeline'];
    onPauseResume: TaskDetailModalProps['onPauseResume'];
}

function DetailsTab({ task, tasks, dag, childPipeline, serverOffsetMs, onTaskSelect, onOpenPipeline, onPauseResume }: DetailsTabProps) {
    // Team-tier task-modal sub-components (absent in the OSS build) — ADR #99.
    const ConsecutiveProgress = paidSurface.ConsecutiveProgress;
    const DependencyStatusList = paidSurface.DependencyStatusList;
    return (
        <div className="td-task-details-grid">
            {/* Paused task message */}
            {task.status === 'waiting_paused' && (
                    <div className="td-paused-task-message">
                        <div className="td-paused-task-icon"><Pause size={24} className="text-amber-500" /></div>
                        <div className="td-paused-task-text">
                            <strong>Pipeline paused</strong>
                            <p>This task is ready to run but waiting for the pipeline to resume.</p>
                        </div>
                        <Button size="sm" className="bg-green-600 hover:bg-green-700" onClick={onPauseResume}>
                            <Play size={14} /> Resume
                        </Button>
                    </div>
                )}
                
                {/* Duration Stats */}
                <div className="td-duration-stats">
                    <div className="td-duration-stat">
                        <div className="td-duration-value text-accent">
                            <LiveDuration task={task} />
                        </div>
                        <div className="td-duration-label">Duration</div>
                    </div>
                    <div className="td-duration-stat">
                        <div className="td-duration-value" style={{ color: (task.status === 'success' || task.status === 'succeeded') ? 'var(--success)' : task.status === 'failed' ? 'var(--error)' : 'var(--text-muted)' }}>
                            {(task.status === 'success' || task.status === 'succeeded') ? <Check size={16} /> : task.status === 'failed' ? <XIcon size={16} /> : '...'}
                        </div>
                        <div className="td-duration-label">Status</div>
                    </div>
                    <div className="td-duration-stat">
                        <div className="td-duration-value text-secondary">
                            {task.dependencies?.length || 0}
                        </div>
                        <div className="td-duration-label">Dependencies</div>
                    </div>
                </div>
                
                {/* Main details */}
                <div className="detail-columns">
                    <div className="detail-column">
                        <div className="detail-section">
                            <div className="detail-label">Execution Name</div>
                            <div className="detail-value td-mono td-flex-row">
                                <span className="td-ellipsis">{task.execution_name || '-'}</span>
                                {task.execution_name && (
                                    <Button 
                                        variant="ghost" 
                                        size="icon"
                                        className="h-6 w-6 opacity-60 hover:opacity-100"
                                        onClick={() => navigator.clipboard.writeText(task.execution_name)}
                                        title="Copy to clipboard"
                                    ><Copy size={14} /></Button>
                                )}
                            </div>
                        </div>
                        
                        {task.pipeline_execution && (
                            <div className="detail-section">
                                <div className="detail-label">Pipeline Execution</div>
                                <div className="detail-value td-mono text-xs td-flex-row">
                                    <span className="td-ellipsis">
                                        {task.pipeline_execution_short || task.pipeline_execution?.split(':').pop()?.substring(0, 12) || '-'}
                                    </span>
                                    <Button 
                                        variant="ghost" 
                                        size="icon"
                                        className="h-6 w-6 opacity-60 hover:opacity-100"
                                        onClick={() => navigator.clipboard.writeText(task.pipeline_execution ?? '')}
                                        title="Copy full execution ID"
                                    ><Copy size={14} /></Button>
                                </div>
                            </div>
                        )}
                        
                        {task.task_type && (
                            <div className="detail-section">
                                <div className="detail-label">Task Type</div>
                                <div className="detail-value">
                                    <span className="tag td-task-type-tag">
                                        {task.task_type}
                                    </span>
                                </div>
                            </div>
                        )}
                        
                        {childPipeline && onOpenPipeline && (
                            <div className="detail-section">
                                <div className="detail-label">Child Pipeline</div>
                                <div className="detail-value">
                                    <button
                                        onClick={() => onOpenPipeline(childPipeline.name, task.date || null)}
                                        className="td-link-primary td-child-pipeline-btn"
                                        title={`Open ${childPipeline.name} pipeline`}
                                    >
                                        <ExternalLink size={12} /> {childPipeline.name} →
                                    </button>
                                </div>
                            </div>
                        )}
                        
                        <div className="detail-section">
                            <div className="detail-label">Started</div>
                            <div className="detail-value">{formatDate(task.running_at || task.started_at)}</div>
                        </div>
                        
                        {task.running_at && task.started_at && task.running_at !== task.started_at && (
                            <div className="detail-section">
                                <div className="detail-label">Queued</div>
                                <div className="detail-value">{formatDate(task.started_at)}</div>
                            </div>
                        )}
                        
                        <div className="detail-section">
                            <div className="detail-label">Finished</div>
                            <div className="detail-value">{formatDate(task.finished_at)}</div>
                        </div>
                    </div>
                    
                    <div className="detail-column">
                        {/* Wait Before - with live countdown */}
                        <CountdownTimer 
                            waitBefore={Number(task.wait_before || dag?.nodes?.find(n => n.id === task.task_name)?.wait_before || 0)}
                            waitDelayUntilMs={task.wait_delay_until_ms ?? null}
                            waitDelayStartedMs={task.wait_delay_started_ms ?? null}
                            status={task.status}
                            serverOffsetMs={serverOffsetMs}
                        />
                        
                        {DependencyStatusList && (
                        <div className="detail-section">
                            <div className="detail-label">Dependencies</div>
                            <DependencyStatusList task={task} tasks={tasks} onTaskSelect={onTaskSelect} />
                        </div>
                        )}
                        
                        {/* Asset Dependencies (wait_for) */}
                        {task.wait_for && (() => {
                            try {
                                const waitFor = typeof task.wait_for === 'string' 
                                    ? JSON.parse(task.wait_for) 
                                    : task.wait_for;
                                if (Array.isArray(waitFor) && waitFor.length > 0) {
                                    return (
                                        <div className="detail-section">
                                            <div className="detail-label"><Database size={12} /> Asset Dependencies</div>
                                            <div className="td-deps-list">
                                                {waitFor.map((asset, idx) => {
                                                    const name = asset.asset_name || asset.name || 'unknown';
                                                    const freshness = asset.freshness_hours;
                                                    const consecutive = asset.consecutive_days;
                                                    const title = consecutive 
                                                        ? `Requires ${consecutive} consecutive days of data`
                                                        : freshness 
                                                            ? `Must be fresh within ${formatFreshnessWindowLong(freshness)}` 
                                                            : 'Latest available';
                                                    return (
                                                        <span 
                                                            key={idx} 
                                                            className="td-dep-tag td-asset-dep" 
                                                            title={title}
                                                        >
                                                            <Database size={12} /> 
                                                            {name}
                                                            {freshness && <span className="td-freshness-badge"><Hourglass size={10} /> {formatFreshnessWindow(freshness)}</span>}
                                                            {consecutive && <span className="td-freshness-badge"><Calendar size={10} /> {consecutive}d</span>}
                                                        </span>
                                                    );
                                                })}
                                            </div>
                                            {waitFor.some(a => a.consecutive_days) && ConsecutiveProgress && (
                                                <ConsecutiveProgress 
                                                    waitFor={waitFor} 
                                                    referenceDate={task.date ?? ''} 
                                                />
                                            )}
                                        </div>
                                    );
                                }
                            } catch (e: unknown) {
                                logger.warn('TaskDetail', 'Failed to parse wait_for', e);
                            }
                            return null;
                        })()}
                        
                        {(task.trigger_rule && task.trigger_rule !== 'all_success') && (
                            <div className="detail-section">
                                <div className="detail-label"><Target size={12} /> Trigger Rule</div>
                                <div className="detail-value text-mono">{task.trigger_rule}</div>
                            </div>
                        )}
                        
                        {/* AWS Console Links */}
                        {(task.task_execution_arn || task.wrapper_execution_arn) && (
                            <div className="detail-section">
                                <div className="detail-label">AWS Console</div>
                                <div className="flex flex-col gap-xs mt-sm">
                                    {/* buildAwsConsoleUrl only builds a Step Functions console
                                        URL, so the direct "Task" link is correct only when the
                                        task's execution ARN is itself a Step Functions execution
                                        (SFN tasks). Other task types (ECS / Glue / Athena / …)
                                        have no states ARN, so they get only the Wrapper link —
                                        AWS's own console surfaces the real resource link on the
                                        wrapper execution page. */}
                                    {task.task_execution_arn?.includes(':states:') && (
                                        <a 
                                            href={buildAwsConsoleUrl(task.task_execution_arn)} 
                                            target="_blank" 
                                            rel="noopener noreferrer"
                                            className="td-link-primary"
                                        >
                                            <FileText size={12} /> Task
                                            <ExternalLink size={10} className="opacity-50" />
                                        </a>
                                    )}
                                    {task.wrapper_execution_arn && (
                                        <a 
                                            href={buildAwsConsoleUrl(task.wrapper_execution_arn)} 
                                            target="_blank" 
                                            rel="noopener noreferrer"
                                            className="td-link-primary"
                                        >
                                            <RotateCcw size={12} /> Wrapper
                                            <ExternalLink size={10} className="opacity-50" />
                                        </a>
                                    )}
                                    {task.pagerduty_enabled && task.wrapper_arn && (
                                        <a 
                                            href={`https://app.pagerduty.com/incidents?search=${encodeURIComponent(task.wrapper_arn)}`}
                                            target="_blank" 
                                            rel="noopener noreferrer"
                                            className="td-link-muted"
                                        >
                                            <AlertTriangle size={12} /> PagerDuty
                                            <ExternalLink size={10} className="opacity-50" />
                                        </a>
                                    )}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
                
                {task.error && (
                    <div className="detail-section td-error-section">
                        <div className="detail-label td-flex-between">
                            <span>Error</span>
                            <Button 
                                variant="ghost" 
                                size="icon"
                                className="h-6 w-6 opacity-60 hover:opacity-100"
                                onClick={() => navigator.clipboard.writeText(typeof task.error === 'string' ? task.error : JSON.stringify(task.error ?? ''))}
                                title="Copy error"
                            ><Copy size={14} /></Button>
                        </div>
                        <ErrorDisplay error={task.error} />
                    </div>
                )}
            </div>
    );
}

// =============================================================================
// Timeline Tab
// =============================================================================

interface TimelineTabProps {
    task: Task;
    taskEvents: TaskDetailModalProps['taskEvents'];
    taskEventsLoading: boolean;
}

function TimelineTab({ task, taskEvents, taskEventsLoading }: TimelineTabProps) {
    return (
        <div className="td-status-history-container">
            {taskEventsLoading ? (
                <div className="td-status-history-loading">Loading events...</div>
            ) : taskEvents.length > 0 ? (
                <>
                    <div className="td-status-history-disclaimer success">
                        <Check size={14} className="inline mr-1" /> Real events from task execution ({taskEvents.length} events)
                    </div>
                    <div className="td-status-history-events">
                        {taskEvents.map((evt, idx) => (
                            <div key={idx} className="td-status-event">
                                <span className="td-event-time">{formatDate(evt.event_time)}</span>
                                <span className={`td-event-badge td-event-${evt.event_type.toLowerCase().replace('_', '-')}`}>
                                    {evt.event_type.replace('_', ' ')}
                                </span>
                                <span className="td-event-text">
                                    {evt.event_type === 'WRAPPER_STARTED' && `Task wrapper started (attempt ${evt.attempt || 1})`}
                                    {evt.event_type === 'DEPS_READY' && `Dependencies satisfied${evt.dependencies ? `: ${evt.dependencies}` : ''}`}
                                    {evt.event_type === 'DEPS_BLOCKED' && `Blocked: ${evt.reason || 'upstream failed'}`}
                                    {evt.event_type === 'TASK_STARTED' && `${evt.task_type || 'Task'} execution started${evt.task_arn ? ` (${evt.task_arn.split(':').pop()})` : ''}`}
                                    {evt.event_type === 'TASK_FINISHED' && evt.status === 'success' && 'Task completed successfully'}
                                    {evt.event_type === 'TASK_FINISHED' && evt.status === 'failed' && `Task failed${evt.error_summary ? `: ${evt.error_summary}` : ''}`}
                                    {evt.event_type === 'MANUAL_DECISION' && <><User size={12} className="inline mr-1" />{evt.decision?.toUpperCase() || 'Action'}: {evt.reason || 'via UI'}</>}
                                    {!['WRAPPER_STARTED', 'DEPS_READY', 'DEPS_BLOCKED', 'TASK_STARTED', 'TASK_FINISHED', 'MANUAL_DECISION'].includes(evt.event_type) && evt.event_type}
                                </span>
                            </div>
                        ))}
                    </div>
                </>
            ) : (
                <>
                    <div className="td-status-history-disclaimer">
                        <Info size={14} className="inline mr-1" /> Derived from task status (no real events yet)
                    </div>
                    <DerivedTimeline task={task} />
                </>
            )}
        </div>
    );
}

/** Status-derived timeline when no real events are available */
function DerivedTimeline({ task }: { task: Task }) {
    const events: Array<{ time: string; badge: string; badgeClass: string; text: string }> = [];

    if (task.started_at) {
        events.push({ time: formatDate(task.started_at), badge: 'Started', badgeClass: 'td-event-started', text: 'Task execution began' });
    }

    const statusEvents: Record<string, { badge: string; badgeClass: string; text: string; useTime?: string }> = {
        'waiting': { badge: 'Waiting', badgeClass: 'td-event-waiting', text: 'Waiting for upstream dependencies' },
        'deps_ready': { badge: 'Ready', badgeClass: 'td-event-ready', text: 'Dependencies satisfied, queued for execution' },
        'waiting_delay': { badge: 'Delayed', badgeClass: 'td-event-waiting', text: 'Waiting for delay timer' },
        'running': { badge: 'Running', badgeClass: 'td-event-running', text: 'Task is currently executing' },
        'success': { badge: 'Success', badgeClass: 'td-event-success', text: 'Task completed successfully', useTime: task.finished_at },
        'succeeded': { badge: 'Success', badgeClass: 'td-event-success', text: 'Task completed successfully', useTime: task.finished_at },
        'failed': { badge: 'Failed', badgeClass: 'td-event-failed', text: typeof task.error === 'string' ? task.error : task.error ? JSON.stringify(task.error) : 'Task execution failed', useTime: task.finished_at },
        'skipped': { badge: 'Skipped', badgeClass: 'td-event-skipped', text: 'Task was skipped', useTime: task.finished_at },
        'upstream_failed': { badge: 'Upstream Failed', badgeClass: 'td-event-failed', text: 'An upstream dependency failed', useTime: task.finished_at },
        'stopped': { badge: 'Stopped', badgeClass: 'td-event-stopped', text: 'Task was manually stopped', useTime: task.finished_at },
        'aborted': { badge: 'Aborted', badgeClass: 'td-event-aborted', text: 'Task was aborted due to pipeline failure', useTime: task.finished_at },
        'waiting_paused': { badge: 'Paused', badgeClass: 'td-event-waiting', text: 'Pipeline is paused' },
        'waiting_decision': { badge: 'Decision', badgeClass: 'td-event-waiting', text: 'Awaiting manual decision' },
    };

    const evt = statusEvents[task.status];
    if (evt) {
        events.push({ time: evt.useTime ? formatDate(evt.useTime) : 'now', badge: evt.badge, badgeClass: evt.badgeClass, text: evt.text });
    }

    return (
        <div className="td-status-history-events">
            {events.map((e, idx) => (
                <div key={idx} className="td-status-event">
                    <span className="td-event-time">{e.time}</span>
                    <span className={`td-event-badge ${e.badgeClass}`}>{e.badge}</span>
                    <span className="td-event-text">{e.text}</span>
                </div>
            ))}
        </div>
    );
}

// =============================================================================
// Actions Tab
// =============================================================================

interface ActionsTabProps {
    task: Task;
    upstreamCount: number;
    downstreamCount: number;
    onAction?: (action: string, taskName?: string) => void;
    onRunAction?: (action: string, task: Task) => void;
    onClose: () => void;
}

interface OutputTabProps {
    input: unknown;
    output: unknown;
    truncated: boolean;
    loading: boolean;
    loaded: boolean;
}

function OutputTab({ input, output, truncated, loading, loaded }: OutputTabProps) {
    if (loading) {
        return <div className="td-tab-empty"><Hourglass size={16} /> Loading…</div>;
    }
    if (!loaded) {
        return <div className="td-tab-empty"><Database size={16} /> Open to load input and output.</div>;
    }

    const hasInput = input !== null && input !== undefined;
    const hasOutput = output !== null && output !== undefined;

    return (
        <div className="td-output-tab">
            <div className="td-io-section">
                <div className="td-io-label">Input</div>
                {hasInput ? (
                    <pre className="td-output-json" aria-label="Task input">
                        {JSON.stringify(input, null, 2)}
                    </pre>
                ) : (
                    <div className="td-tab-empty td-tab-empty--inline">
                        <Database size={14} /> No input recorded (upstream data + run variables).
                    </div>
                )}
            </div>
            <div className="td-io-section">
                <div className="td-io-label">Output</div>
                {truncated ? (
                    <div className="td-tab-empty td-tab-empty--inline" role="status">
                        <AlertTriangle size={14} /> Output too large to store inline. Return an
                        <code>s3://</code> pointer for large data.
                    </div>
                ) : hasOutput ? (
                    <pre className="td-output-json" aria-label="Task output">
                        {JSON.stringify(output, null, 2)}
                    </pre>
                ) : (
                    <div className="td-tab-empty td-tab-empty--inline">
                        <Database size={14} /> This task stored no output.
                    </div>
                )}
            </div>
        </div>
    );
}

function ActionsTab({ task, upstreamCount, downstreamCount, onAction, onRunAction, onClose }: ActionsTabProps) {
    const openBackfillModal = useAppStore((s) => s.openBackfillModal);
    return (
        <div className="actions-tab-content">
            {/* Task State Actions */}
            <div className="action-group">
                <div className="action-group-title">Task Control</div>
                <div className="action-buttons">
                    {(task.status === 'waiting_decision' || task.status === 'failed' || task.status === 'waiting' || (task.status === 'running' && task.error)) && (
                        <button className="action-btn warning" onClick={() => onAction?.('skip')}>
                            <span className="action-btn-icon"><SkipForward size={18} /></span>
                            <span className="action-btn-text">
                                <strong>Skip Task</strong>
                                <small>Mark as complete, trigger dependents</small>
                            </span>
                        </button>
                    )}
                    {(task.status === 'waiting_decision' || (task.status === 'running' && task.error)) && (
                        <button className="action-btn danger" onClick={() => onAction?.('fail')}>
                            <span className="action-btn-icon"><XCircle size={18} /></span>
                            <span className="action-btn-text">
                                <strong>Mark Failed</strong>
                                <small>Fail this task and pipeline</small>
                            </span>
                        </button>
                    )}
                    {(task.status === 'waiting_decision' || task.status === 'running' || task.status === 'waiting' || task.status === 'stopped' || task.status === 'failed') && (
                        <button className="action-btn success" onClick={() => onAction?.('success')}>
                            <span className="action-btn-icon"><CheckCircle2 size={18} /></span>
                            <span className="action-btn-text">
                                <strong>Mark Successful</strong>
                                <small>Force complete if work done</small>
                            </span>
                        </button>
                    )}
                    {((task.status === 'running' && !task.error) || task.status === 'waiting_decision') && (
                        <button className="action-btn secondary" onClick={() => onAction?.('stop')}>
                            <span className="action-btn-icon"><StopCircle size={18} /></span>
                            <span className="action-btn-text">
                                <strong>Stop Task</strong>
                                <small>Stop execution (can restart later)</small>
                            </span>
                        </button>
                    )}
                    {(TASK_SETTLED_STATUSES.includes(task.status) || task.status === 'waiting_decision') && (
                        <button className="action-btn primary" onClick={() => onAction?.('restart')}>
                            <span className="action-btn-icon"><RotateCcw size={18} /></span>
                            <span className="action-btn-text">
                                <strong>Restart Task</strong>
                                <small>Run this task again</small>
                            </span>
                        </button>
                    )}
                </div>
            </div>
            
            {/* Run Actions */}
            <div className="action-group">
                <div className="action-group-title">Pipeline Run</div>
                <div className="action-buttons">
                    <button className="action-btn" onClick={() => { onClose(); onRunAction?.('toHere', task); }}>
                        <span className="action-btn-icon"><Target size={18} /></span>
                        <span className="action-btn-text">
                            <strong>Run to Here</strong>
                            <small>Run this + {upstreamCount} upstream tasks</small>
                        </span>
                    </button>
                    <button className="action-btn" onClick={() => { onClose(); onRunAction?.('fromHere', task); }}>
                        <span className="action-btn-icon"><Play size={18} /></span>
                        <span className="action-btn-text">
                            <strong>Run from Here</strong>
                            <small>Run this + {downstreamCount} downstream tasks</small>
                        </span>
                    </button>
                    <button className="action-btn" onClick={() => { onClose(); onRunAction?.('onlyThis', task); }}>
                        <span className="action-btn-icon"><CircleDot size={18} /></span>
                        <span className="action-btn-text">
                            <strong>Run Only This</strong>
                            <small>Run just this task, skip others</small>
                        </span>
                    </button>
                </div>
            </div>

            {/* Backfill Actions (v0.78+, ADR #51) — Team-only paid surface, same
                gate as the pipeline Backfill button. Hidden in the free (OSS) build. */}
            {paidSurface.BackfillNavTab && (
            <div className="action-group">
                <div className="action-group-title">Backfill</div>
                <div className="action-buttons">
                    <button
                        className="action-btn"
                        onClick={() => {
                            onClose();
                            openBackfillModal({
                                origin: 'task-detail',
                                target: { type: 'pipeline', name: task.pipeline_name },
                                tasks: [task.task_name],
                            });
                        }}
                    >
                        <span className="action-btn-icon"><Rocket size={18} /></span>
                        <span className="action-btn-text">
                            <strong>Backfill This Task</strong>
                            <small>Run this task across a date range</small>
                        </span>
                    </button>
                </div>
            </div>
            )}
        </div>
    );
}

export default TaskDetailModal;
