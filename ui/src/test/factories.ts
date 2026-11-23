/**
 * Test Factories - Shared mock data builders for UI component tests.
 * 
 * Each factory returns a minimal valid object with sensible defaults.
 * Override any field by passing a partial object.
 * 
 * This file is the SINGLE SOURCE of mock data for all component tests.
 * Hook tests may have their own inline mocks, but component tests should use these.
 */

// ─── Task ────────────────────────────────────────────────────────────────────

export const createTask = (overrides = {}) => ({
    task_name: 'extract_data',
    status: 'success',
    pipeline_name: 'acme-daily',
    execution_name: 'exec-2024-01-15-001',
    pipeline_execution: 'arn:aws:states:us-east-1:123456:execution:pipeline:abc123',
    pipeline_execution_short: 'abc123',
    task_type: 'lambda',
    task_arn: 'arn:aws:lambda:us-east-1:123456:function:extract',
    run_task_arn: 'arn:aws:states:us-east-1:123456:execution:acme-dev-polyris-run-task-helper:run001',
    dependencies: ['init_config'],
    started_at: '2024-01-15T08:00:00Z',
    running_at: '2024-01-15T08:00:05Z',
    finished_at: '2024-01-15T08:05:30Z',
    error: null,
    notification_failed: false,
    ...overrides,
});

export const createFailedTask = (overrides = {}) => createTask({
    status: 'failed',
    error: 'Lambda function timed out after 300s',
    finished_at: '2024-01-15T08:05:30Z',
    ...overrides,
});

export const createRunningTask = (overrides = {}) => createTask({
    status: 'running',
    finished_at: null,
    error: null,
    ...overrides,
});

export const createWaitingDecisionTask = (overrides = {}) => createTask({
    status: 'waiting_decision',
    finished_at: null,
    error: 'Lambda function timed out after 300s',
    ...overrides,
});

export const createPausedTask = (overrides = {}) => createTask({
    status: 'waiting_paused',
    finished_at: null,
    error: null,
    ...overrides,
});

export const createStoppedTask = (overrides = {}) => createTask({
    status: 'stopped',
    finished_at: '2024-01-15T08:03:00Z',
    ...overrides,
});

// ─── DAG ─────────────────────────────────────────────────────────────────────

export const createDAG = (overrides = {}) => ({
    nodes: [
        { id: 'init_config', outlets: [], inlets: [], skip_on_backfill: false },
        { id: 'extract_data', outlets: ['inventory'], inlets: [], skip_on_backfill: false },
        { id: 'transform', outlets: [], inlets: ['inventory'], skip_on_backfill: false },
        { id: 'load_db', outlets: [], inlets: [], skip_on_backfill: false },
    ],
    edges: [
        { from: 'init_config', to: 'extract_data' },
        { from: 'extract_data', to: 'transform' },
        { from: 'transform', to: 'load_db' },
    ],
    ...overrides,
});

// ─── Pipeline ────────────────────────────────────────────────────────────────

export const createPipeline = (overrides = {}) => ({
    name: 'acme-daily',
    arn: 'arn:aws:states:us-east-1:123456:stateMachine:acme-daily',
    status: 'success',
    group: null,
    last_run: '2024-01-15T08:00:00Z',
    schedule: 'daily',
    task_count: 4,
    ...overrides,
});

export const createPipelines = (count = 5) => {
    const names = ['acme-daily', 'shopmart-weekly', 'shared-feeds', 'nexus-hourly', 'vertex-daily'];
    const groups = ['acme', 'shopmart', 'shared', 'nexus', 'vertex'];
    const statuses = ['success', 'running', 'failed', 'waiting', 'success'];
    return Array.from({ length: count }, (_, i) => createPipeline({
        name: names[i % names.length],
        group: groups[i % groups.length],
        status: statuses[i % statuses.length],
    }));
};

// ─── Task Events ─────────────────────────────────────────────────────────────

export const createTaskEvents = () => [
    { event_time: '2024-01-15T08:00:02Z', event_type: 'DEPS_READY', dependencies: 'init_config' },
    { event_time: '2024-01-15T08:00:05Z', event_type: 'TASK_STARTED', task_type: 'lambda', task_arn: 'arn:aws:lambda:us-east-1:123456:function:extract' },
    { event_time: '2024-01-15T08:05:30Z', event_type: 'TASK_FINISHED', status: 'success' },
];

// ─── Executions ──────────────────────────────────────────────────────────────

export const createExecution = (overrides = {}) => ({
    execution_id: 'exec-2024-01-15-001',
    pipeline_name: 'acme-daily',
    status: 'success',
    started_at: '2024-01-15T08:00:00Z',
    finished_at: '2024-01-15T08:10:00Z',
    task_count: 4,
    ...overrides,
});

// ─── Default Props Builders ──────────────────────────────────────────────────

export const createTaskDetailModalProps = (overrides = {}) => ({
    task: createTask(),
    tasks: [createTask(), createTask({ task_name: 'transform', status: 'waiting' })],
    dag: createDAG(),
    pipelines: [createPipeline()],
    taskEvents: [],
    taskEventsLoading: false,
    onClose: vi.fn(),
    onAction: vi.fn(),
    onRunAction: vi.fn(),
    onTaskSelect: vi.fn(),
    onOpenPipeline: vi.fn(),
    onPauseResume: vi.fn(),
    serverOffsetMs: 0,
    ...overrides,
});

export const createCommandPaletteProps = (overrides = {}) => ({
    isOpen: true,
    onClose: vi.fn(),
    pipelines: createPipelines(5),
    onSelectPipeline: vi.fn(),
    onNavigate: vi.fn(),
    onToggleTheme: vi.fn(),
    theme: 'light',
    ...overrides,
});

// ─── ConfirmModal ────────────────────────────────────────────────────────────

export const createConfirmModalProps = (overrides = {}) => ({
    isOpen: true,
    title: 'Confirm Action',
    message: 'Are you sure you want to proceed?',
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
    confirmText: 'Confirm',
    cancelText: 'Cancel',
    danger: false,
    ...overrides,
});

// ─── AssetTriggerModal ───────────────────────────────────────────────────────

export const createAssetTriggerModalProps = (overrides = {}) => ({
    isOpen: true,
    onClose: vi.fn(),
    assetName: 'inventory_snapshot',
    onTrigger: vi.fn().mockResolvedValue({}),
    ...overrides,
});

// ─── AssetDetailModal ────────────────────────────────────────────────────────

export const createAsset = (overrides = {}) => ({
    uri: 's3://data-lake/inventory',
    producers: ['extract_data'],
    consumers: ['transform', 'load_db'],
    tags: ['retail', 'daily'],
    metadata: { row_count: 150000, format: 'parquet' },
    ...overrides,
});

export const createAssetEvent = (overrides = {}) => ({
    event_time: '2024-01-15T08:05:30Z',
    source_dag: 'acme-daily',
    source_task: 'extract_data',
    execution_date: '2024-01-15',
    ...overrides,
});

export const createAssetDetailModalProps = (overrides = {}) => ({
    asset: createAsset(),
    assetName: 'inventory_snapshot',
    events: [createAssetEvent(), createAssetEvent({ event_time: '2024-01-14T08:05:30Z', execution_date: '2024-01-14' })],
    dagTriggers: {
        'acme-transform': { assets: ['inventory_snapshot'], operator: 'all' },
    },
    staleness: { status: 'fresh', label: '2 hours ago' },
    onClose: vi.fn(),
    onTrigger: vi.fn(),
    onBackfill: vi.fn(),
    onDelete: vi.fn(),
    ...overrides,
});

// ─── Notifications ───────────────────────────────────────────────────────────

export const createNotification = (overrides = {}) => ({
    id: `notif-${Math.random().toString(36).slice(2, 8)}`,
    type: 'failure',
    pipeline_name: 'acme-daily',
    task_name: 'extract_data',
    pipeline_execution: 'arn:aws:states:us-east-1:123456:execution:pipeline:abc123',
    time_ago: '5 minutes ago',
    ...overrides,
});

// ─── /api/health (System status) ─────────────────────────────────────────────
// The shapes routes/health.py answers, and what api.get makes of them.

type HealthWords = { dynamodb?: unknown; stepfunctions?: unknown; coordination?: unknown };

export const HEALTH_CHECKED_AT = '2026-11-14T11:58:00.123Z';

/** A finding as the backend writes it (contract §2, clause 3). */
export const createFinding = (overrides = {}) => ({
    title: 'Asset event delivery is off',
    resource: 'The stream mapping of polyris-dev-polyris-asset-fanout is Disabled.',
    changed_at: '2026-11-14T11:20:00.000Z',
    impact: 'Tasks that wait for an asset start up to 40 minutes late, and pipelines scheduled on an asset up to an hour late.',
    fix: 'Re-enable the mapping: in the Lambda console, open polyris-dev-polyris-asset-fanout, then Configuration → Triggers.',
    command: 'aws lambda update-event-source-mapping --uuid 48529b7b-abda-4306-8550-8d3bbc1391c3 --enabled --region us-east-1',
    ...overrides,
});

/** The full body: a caller with a valid token, or any caller while auth is off. */
export const createHealthBody = (words: HealthWords = {}, findings: unknown[] = []) => ({
    status: 'healthy',
    timestamp: '2026-11-14T12:00:00.000Z',
    response_time_ms: 41.2,
    detail: 'full',
    checks: {
        dynamodb: { status: words.dynamodb ?? 'healthy', latency_ms: 12.5 },
        stepfunctions: { status: words.stepfunctions ?? 'healthy', latency_ms: 80.1 },
        recent_failures: { status: 'healthy', failures_last_hour: 0 },
        circuit_breakers: { status: 'healthy', message: 'Circuit breakers not configured' },
        coordination: { status: words.coordination ?? 'healthy', report: { checked_at: HEALTH_CHECKED_AT, fanout_mapping: 'Enabled' } },
    },
    findings,
});

/** The minimal body: anyone else gets the status words only. */
export const createMinimalHealthBody = (words: HealthWords = {}) => ({
    status: 'healthy',
    timestamp: '2026-11-14T12:00:00.000Z',
    response_time_ms: 41.2,
    detail: 'minimal',
    checks: {
        dynamodb: { status: words.dynamodb ?? 'healthy' },
        stepfunctions: { status: words.stepfunctions ?? 'healthy' },
        recent_failures: { status: 'healthy' },
        circuit_breakers: { status: 'healthy' },
        coordination: { status: words.coordination ?? 'healthy' },
    },
});

/** api.get's result for a 2xx: the body under `data`, and spread. */
export const apiAnswered = (body: object) => ({ ok: true, data: body, ...body });

/** api.get's result for a failure; `body` only when the answer had a JSON one. */
export const apiFailed = (error: string, status?: number, body?: unknown) =>
    ({ ok: false, error, status, ...(body === undefined ? {} : { body }) });
