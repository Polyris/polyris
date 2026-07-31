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
    wrapper_execution_arn: 'arn:aws:states:us-east-1:123456:execution:wrapper:wrap001',
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
    { event_time: '2024-01-15T08:00:00Z', event_type: 'WRAPPER_STARTED', attempt: 1 },
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

export const createPipelinesSidebarProps = (overrides = {}) => ({
    pipelines: createPipelines(5),
    selectedPipeline: null,
    onSelectPipeline: vi.fn(),
    loading: false,
    search: '',
    onSearchChange: vi.fn(),
    isOpen: false,
    onClose: vi.fn(),
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
