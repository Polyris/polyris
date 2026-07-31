// =============================================================================
// polyris Console — TypeScript Type Definitions
// =============================================================================

// --- Task & Pipeline Status ---
//
// v0.79.0 (ADR #72) — enum families now live in src/generated/enums.ts,
// generated from polyris/constants.py by polyris.codegen.sync_enums. This
// file re-exports them for backward-compatible import paths. Don't edit
// the unions here — edit polyris/constants.py and re-run codegen.

import type {
    TaskStatus,
    TriggerRule,
    PipelineStatus,
    ExecutionStatus,
    BackfillStatus,
    BackfillCascade,
    BackfillUpstream,
    BackfillGranularity,
    StalenessStatus,
} from '@/generated/enums';

export type {
    TaskStatus,
    TriggerRule,
    PipelineStatus,
    ExecutionStatus,
    BackfillStatus,
    BackfillCascade,
    BackfillUpstream,
    BackfillGranularity,
    StalenessStatus,
};
export {
    BACKFILL_TERMINAL_STATUSES,
    TASK_TERMINAL_STATUSES,
} from '@/generated/enums';

// --- Core Models ---

export interface Task {
  task_name: string;
  status: TaskStatus;
  pipeline_name: string;
  execution_name: string;
  pipeline_execution_short?: string;
  pipeline_execution?: string;
  dependencies?: string[];
  wait_for?: string | WaitForAsset[];
  wait_before?: number;
  wait_delay_until_ms?: number;
  wait_delay_started_ms?: number;
  trigger_rule?: string;
  started_at?: string;
  ended_at?: string;
  finished_at?: string;
  running_at?: string;
  date?: string;
  duration_ms?: number;
  error?: TaskError | string | null;
  retries?: number;
  max_retries?: number;
  tags?: string[] | Record<string, string>;
  lambda_arn?: string;
  wrapper_arn?: string;
  wrapper_execution_arn?: string;
  task_arn?: string;
  task_execution_arn?: string;
  task_type?: string;
  is_decision_task?: boolean;
  pagerduty_enabled?: boolean;
  slack_notification_failed?: boolean;
  notification_failed?: boolean;
  attempt?: number;
  [key: string]: unknown;
}

export interface TaskError {
  Error?: string;
  Cause?: string;
}

export interface WaitForAsset {
  asset_name?: string;
  name?: string;
}

export interface AssetScheduleInfo {
  operator: 'AND' | 'OR';
  assets: string[];
}

export interface Pipeline {
  name: string;
  description?: string;
  schedule?: string;
  asset_schedule?: AssetScheduleInfo | null;
  is_paused?: boolean;
  task_count?: number;
  stats?: PipelineStats;
  tags?: string[] | Record<string, string>;
  [key: string]: unknown;
}

export interface PipelineStats {
  success: number;
  failed: number;
  running: number;
  waiting: number;
  total: number;
}

export interface Execution {
  execution_id: string;
  execution_arn?: string;
  pipeline_name: string;
  pipeline_execution?: string;
  pipeline_execution_short?: string;
  execution_short?: string;
  status: ExecutionStatus;
  started_at: string;
  ended_at?: string;
  duration_ms?: number;
  date?: string;
  /** Backfill membership (v0.78+, ADR #51). Empty/undefined if standalone. */
  backfill_id?: string;
  partition_key?: string;
  [key: string]: unknown;
}

/**
 * Unified Run/Activity feed row (ADR #95). `/api/runs` now returns a mixed list
 * of pipeline executions and Backfills, discriminated by `kind`. Modeled as an
 * intersection over Execution so it stays assignable to `Execution` (no call-
 * site churn); backfill-only fields are present when `kind === 'backfill'`.
 */
export type RunFeedRow = Execution & {
  kind?: 'execution' | 'backfill';
  /** Backfill record id (present when kind === 'backfill'). */
  id?: string;
  total_partitions?: number;
  completed_partitions?: number;
  failed_partitions?: number;
  skipped_partitions?: number;
  started_by?: string | null;
  downstream?: string | null;
  granularity?: string | null;
};

export interface SelectedExecution {
  execution_id: string;
  execution_short?: string;
  auto_selected?: boolean;
  date?: string;
  [key: string]: unknown;
}

// --- DAG ---

export interface DAGNode {
  id: string;
  label?: string;
  status?: TaskStatus;
  wait_before?: number;
  outlets?: Array<string | { name: string }>;
  inlets?: Array<string | { name: string }>;
  skip_on_backfill?: boolean;
  [key: string]: unknown;
}

export interface DAGEdge {
  from: string;
  to: string;
}

export interface DAG {
  nodes: DAGNode[];
  edges: DAGEdge[];
  [key: string]: unknown;
}

// --- Assets ---

export interface Asset {
  asset_name: string;
  group?: string;
  status?: string;
  last_updated?: string;
  consumers?: string[];
  producers?: string[];
  staleness?: StalenessResult;
}

export interface AssetEvent {
  asset_name: string;
  event_time: string;
  event_type?: string;
  pipeline_name?: string;
  task_name?: string;
  source_task?: string;
  source_dag?: string;
  execution_date?: string;
  metadata?: Record<string, unknown>;
}

export interface StalenessResult {
  status: StalenessStatus;
  label: string;
  hours: number | null;
}

// --- API ---

export type ApiResult<T = unknown> =
  | { ok: true; data: T; [key: string]: unknown }
  | { ok: false; error: string; status?: number };

export interface Notification {
  id: string;
  type: 'error' | 'warning' | 'info' | 'success' | 'failure' | 'backfill' | 'decision_required';
  message?: string;
  pipeline_name?: string;
  task_name?: string;
  pipeline_execution?: string;
  timestamp?: string;
  time_ago?: string;
  acknowledged?: boolean;
  /** ADR #68 — backfill terminal notifications. Populated when type='backfill'. */
  backfill_status?: 'completed' | 'failed' | 'partial' | 'canceled';
  backfill_id?: string;
  target_pipeline?: string;
  total_partitions?: number;
  completed_partitions?: number;
  failed_partitions?: number;
  finished_at?: string;
}

// --- Countdown ---

export interface CountdownState {
  remainingSeconds: number;
  isCountingDown: boolean;
  isCompleted: boolean;
  isPending: boolean;
  progressPercent: number;
}

export interface WaitBadge {
  type: 'countdown' | 'pending' | 'complete';
  text: string;
  icon: 'hourglass' | 'clock' | 'check';
}

// --- Config ---

export interface AppConfig {
  API_URL: string;
  POLL_INTERVAL: number;
  AUTH: AuthConfig;
}

export interface AuthConfig {
  enabled: boolean;
  userPoolId: string;
  clientId: string;
  region: string;
}

// --- Views ---

export type MainView = 'pipelines' | 'assets' | 'tasks' | 'runs' | 'backfills';
export type ViewMode = 'dag' | 'gantt' | 'table' | 'calendar';

const MAIN_VIEWS: readonly string[] = ['pipelines', 'assets', 'tasks', 'runs', 'backfills'];
const VIEW_MODES: readonly string[] = ['dag', 'gantt', 'table', 'calendar'];

export const isMainView = (v: string): v is MainView => MAIN_VIEWS.includes(v);
export const isViewMode = (v: string): v is ViewMode => VIEW_MODES.includes(v);

// --- Toast ---

export type ToastType = 'success' | 'error' | 'warning' | 'info';

export interface ToastMessage {
  id: number;
  message: string;
  type: ToastType;
  duration: number;
}

// =============================================================================
// Component Props
// =============================================================================

// --- Modal Props ---

export interface BaseModalProps {
  isOpen: boolean;
  onClose?: () => void;
  title?: string;
  children?: React.ReactNode;
  className?: string;
  size?: 'sm' | 'md' | 'lg' | 'xl';
}

export interface ModalHeaderProps {
  children: React.ReactNode;
  onClose?: () => void;
  icon?: React.ReactNode;
}

export interface ModalBodyProps {
  children: React.ReactNode;
  className?: string;
}

export interface ModalFooterProps {
  children: React.ReactNode;
  className?: string;
}

export interface ConfirmModalProps extends BaseModalProps {
  message?: string;
  onConfirm: (() => void | Promise<void>) | null;
  onCancel: () => void;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  icon?: React.ReactNode;
}

export interface ModalProps extends BaseModalProps {
  message?: string | null;
  icon?: React.ReactNode;
  confirmText?: string;
  confirmVariant?: 'default' | 'destructive' | 'outline' | 'secondary' | 'ghost' | 'link';
  loading?: boolean;
  children?: React.ReactNode;
  onConfirm?: () => void | Promise<void>;
}

// --- Pipeline Props ---

export interface PipelinesSidebarProps {
  pipelines: PipelineWithUI[];
  selectedPipeline: Pipeline | null;
  onSelectPipeline: (pipeline: PipelineWithUI) => void;
  loading?: boolean;
  search?: string;
  onSearchChange?: (value: string) => void;
  isOpen?: boolean;
  onClose?: () => void;
}

export interface PipelineItemProps {
  pipeline: PipelineWithUI;
  selected: boolean;
  onClick: () => void;
}

export interface RunSparklineProps {
  runs: Array<{ status: string; date?: string }>;
}

export interface PipelineWithUI extends Pipeline {
  status?: string;
  group?: string;
  recent_runs?: Array<{ status: string; date?: string }>;
  arn?: string;
  today_stats?: PipelineStats;
}

export interface PipelineDetailProps {
  pipeline: PipelineWithUI | null;
  tasks: Task[];
  dag: DAG | null;
  executions: Execution[];
  selectedExecution: SelectedExecution | null;
  onSelectExecution: (exec: SelectedExecution | null) => void;
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  date: string;
  onDateChange?: (date: string) => void;
  onRun: () => void;
  onBackfill: () => void;
  onPauseResume: () => void;
  onStop: () => void;
  onExtendPause: () => void;
  onTaskSelect: (task: Task) => void;
  selectedTask: Task | null;
  error: string | null;
  isLoading?: boolean;
  serverOffsetMs?: number;
  executionPaused?: boolean;
  onRefresh?: () => void;
  title?: string;
  text?: string;
  icon?: React.ReactNode;
  children?: React.ReactNode;
}

export interface EmptyStateProps {
  icon: React.ReactNode;
  title: string;
  text?: string;
  children?: React.ReactNode;
}

// --- Header Props ---
// Header.tsx defines its own local HeaderProps. The legacy interfaces here
// (with `mainView` field) were obsoleted in v0.71.x when top-level view
// moved to URL pathname.

export interface ViewTabProps {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  /** Optional badge number (e.g. active backfill count) — rendered as
   * a small pill next to label. Falsy values (0, undefined) hide it. */
  badge?: number | null;
}

// --- DAG Props ---

export interface DAGGraphFlowProps {
  dag: DAG | null;
  tasks: Task[];
  selectedTask: Task | null;
  onSelectTask: (task: Task) => void;
  serverOffsetMs?: number;
  isBlueprint?: boolean;
}

export interface DAGTaskNodeData {
  label: string;
  status: string;
  task: Task | null;
  isBlueprint: boolean;
  serverOffsetMs: number;
  onClick: () => void;
  duration?: number;
}

export interface CountdownBadgeProps {
  task: Task | null;
  serverOffsetMs: number;
}

// --- Asset Props ---

export interface AssetLineageFlowProps {
  assets: Record<string, AssetData> | null;
  dagTriggers: Record<string, DagTriggerData>;
  recentEvents?: AssetEvent[];
  onSelectAsset?: (name: string) => void;
  selectedAsset?: string | null;
  focusedAsset?: string | null;
  initialDepth?: number;
}

export interface AssetSchemaColumn {
  name: string;
  type: string;
  description?: string;
  // Constraint fields (polyris Column class). All optional with same defaults
  // as the Python side: nullable=true, the rest false. Backend omits fields
  // that match defaults to keep payload compact.
  nullable?: boolean;
  primary_key?: boolean;
  partition_key?: boolean;
  unique?: boolean;
  default?: string | number | boolean | null;
}

export interface AssetSchemaConflict {
  /** The pipeline that declared a divergent schema (the "loser" or
   *  later-declarations after the baseline). */
  pipeline: string;
  /** Column count of the divergent declaration — useful in tooltips
   *  without forcing the full schema to be loaded twice. */
  columns: number;
}

export interface AssetData {
  name?: string;
  group?: string;
  producers?: string[];
  consumers?: string[];
  last_updated?: string;
  staleness?: StalenessResult;
  tags?: string[] | Record<string, string>;
  metadata?: Record<string, unknown>;
  uri?: string;
  owner?: string;
  description?: string;
  schema?: AssetSchemaColumn[];
  glue_table?: string;
  glue_catalog?: string;
  /** AWS region for the Glue Catalog reference. Empty when the asset's
   *  Glue Catalog lives in the same region as the Console API Lambda
   *  (the default). Set when the asset points at a Glue Catalog in a
   *  different region — backend uses this to create a region-pinned
   *  boto3 client for drift-detection calls. */
  glue_region?: string;
  freshness_hours?: number | null;
  /** Cross-pipeline schema conflicts. Empty/undefined when only one
   *  pipeline declared this asset, or when all declarations agreed.
   *  Populated by `_build_assets_from_pipelines` whenever a divergent
   *  schema is encountered for an already-declared asset. The richest
   *  declaration still wins (see `dict_schema_richness`); this field
   *  is for surfacing the conflict to operators. */
  schema_conflicts?: AssetSchemaConflict[];
  [key: string]: unknown;
}

export interface DagTriggerData {
  pipeline_name?: string;
  assets?: string[];
  operator?: string;
}

export interface AssetNodeData {
  label: string;
  fullName?: string;
  group?: string;
  staleness?: StalenessResult;
  stalenessLabel?: string;
  eventCount?: number;
  isSelected?: boolean;
  onClick?: () => void;
  tags?: string[] | Record<string, string>;
}

export interface TaskNodeData {
  label: string;
  type: 'producer' | 'consumer';
  taskName?: string;
  dagId?: string;
  nodeType?: string;
}

export interface DagTriggerNodeData {
  label: string;
  assetCount?: number;
  dagId?: string;
  operator?: string;
}

export interface AssetsViewProps {
  date: string;
  sidebarOpen?: boolean;
  onCloseSidebar?: () => void;
}

export interface AssetDetailModalProps {
  asset: AssetData | null;
  assetName: string;
  events: AssetEvent[];
  dagTriggers: Record<string, DagTriggerData>;
  staleness?: StalenessResult | null;
  onClose: () => void;
  onTrigger: () => void;
  onBackfill: () => void;
  onDelete: (assetName: string) => void | Promise<void>;
  onViewInCatalog?: () => void;
}

export interface RecentEventsPanelProps {
  events: AssetEvent[];
  onSelectAsset: (name: string) => void;
}

// --- Task Detail Props ---

export interface TaskDetailModalProps {
  task: Task | null;
  tasks: Task[];
  dag: DAG | null;
  pipelines: PipelineWithUI[];
  taskEvents: TaskEvent[];
  taskEventsLoading: boolean;
  onClose: () => void;
  onAction?: (action: string, taskName?: string) => void;
  onRunAction?: (action: string, task: Task) => void;
  onTaskSelect: (task: Task) => void;
  onOpenPipeline: (pipelineName: string, date?: string | null) => void;
  onPauseResume?: () => void;
  serverOffsetMs?: number;
}

export interface TaskEvent {
  event_type: string;
  timestamp: string;
  pipeline_name?: string;
  task_name?: string;
  details?: Record<string, unknown>;
  task_arn?: string;
  status?: string;
  reason?: string;
  error_summary?: string;
  dependencies?: string;
  decision?: string;
  attempt?: number;
  task_type?: string;
  event_time?: string;
}

export interface ConsecutiveProgressProps {
  waitFor: WaitForAsset[];
  referenceDate: string;
}

export interface LiveDurationProps {
  task: Task;
}

export interface DependencyStatusListProps {
  task: Task;
  tasks: Task[];
  onTaskSelect: (task: Task) => void;
}

// --- Backfill ---
//
// Pre-v0.78 BackfillModalProps and BackfillPayload were removed in ADR #51
// when the universal seed-driven BackfillModal replaced the per-pipeline
// and per-asset modals. New types live in the "Backfill (v0.78+, per ADR
// #51)" block at the end of this file: BackfillStartRequest,
// BackfillStartResponse, BackfillSummary, BackfillDetail, BackfillModalSeed,
// etc.

// --- GanttChart ---

export interface GanttChartProps {
  tasks: Task[];
  selectedTask: Task | null;
  onSelectTask: (task: Task) => void;
}

// --- Auth ---

export interface LoginFormProps {
  onForgotPassword: () => void;
}

export interface UserMenuProps {
  onManageUsers?: () => void;
}

// --- ErrorBoundary ---

export interface ErrorBoundaryProps {
  children: React.ReactNode;
  fallback?: React.ReactNode;
  onError?: (error: Error, info: React.ErrorInfo) => void;
}

export interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: React.ErrorInfo | null;
  errorCount: number;
}

// --- Filters ---

export interface TaskFilter {
  status: string;
  date: string;
  pipeline: string;
  taskName: string;
}

export interface RunFilter {
  status: string;
  pipeline: string;
  /** The Runs workspace's own date filter — '' means every date (ADR #106).
   *  Not the Pipeline page's `date`: that one scopes a page, this one filters a feed. */
  date: string;
}

// --- AllTasksView / AllRunsView Props ---

export interface AllTasksViewProps {
  tasks: Task[];
  pipelines: PipelineWithUI[];
  filter: TaskFilter;
  onFilterChange: (f: TaskFilter) => void;
  onPipelineClick: (pipeline: PipelineWithUI, date?: string) => void;
  loading?: boolean;
}

export interface AllRunsViewProps {
  runs: Execution[];
  pipelines: PipelineWithUI[];
  filter: RunFilter;
  onFilterChange: (f: RunFilter) => void;
  onPipelineClick: (pipeline: PipelineWithUI, run: Execution) => void;
  onRefresh: () => void;
  loading?: boolean;
}

// --- Backfill (v0.78+, per ADR #51) ---
// BackfillStatus, BackfillCascade, BackfillGranularity re-exported above
// from @/generated/enums (ADR #72).

export interface BackfillTarget {
  type: 'pipeline' | 'asset' | 'batch';
  name?: string;
  items?: BackfillTarget[];
}

export interface BackfillPartitionsRange {
  start: string;
  end: string;
}
export interface BackfillPartitionsKeys {
  keys: string[];
}
export type BackfillPartitions = BackfillPartitionsRange | BackfillPartitionsKeys;

export interface BackfillOptions {
  force?: boolean;
  skip_completed?: boolean;
  incremental?: boolean;
  max_parallel?: number;
  allow_concurrent?: boolean;
  variables?: Record<string, unknown>;
}

/** Upstream lineage build mode (asset target only). ADR #92 / #94. off = just
 * the producer; smart = build missing same-pipeline ancestors; force = full
 * lineage. The union is generated from polyris/constants.py (BackfillUpstream)
 * and re-exported at the top of this file — do not redeclare it here. */

export interface BackfillStartRequest {
  target: BackfillTarget;
  partitions: BackfillPartitions;
  tasks?: string[] | null;
  /** Downstream consumer fan-out (asset target only). ADR #91. */
  downstream?: BackfillCascade;
  /** Upstream lineage build (asset target only). ADR #92. */
  upstream?: BackfillUpstream;
  options?: BackfillOptions;
  /**
   * Override the inferred granularity. Only used when the user has cron
   * ambiguity (cron_was_ambiguous=true in preview) and wants to force a
   * specific cadence different from the default 'daily' fallback.
   */
  granularity_override?: BackfillGranularity;
}

export interface BackfillWarning {
  code: string;
  message: string;
}

export interface BackfillStartResponse {
  backfill_id: string;
  target_pipeline: string;
  granularity_inferred?: BackfillGranularity | null;
  cron?: string | null;
  cron_was_ambiguous?: boolean;
  partition_count_requested: number;
  partition_count_skipped_completed: number;
  partition_count_to_run: number;
  task_subset?: string[] | null;
  downstream?: BackfillCascade | null;
  /** @deprecated use `downstream` (ADR #91) — mirrored for transition. */
  cascade?: BackfillCascade | null;
  warnings: BackfillWarning[];
  ui_url: string;
}

export interface BackfillPreviewResponse {
  preview: true;
  target_pipeline: string;
  granularity: BackfillGranularity;
  granularity_inferred?: BackfillGranularity | null;
  /** The pipeline's schedule expression (cron) — shown next to granularity for context. */
  cron?: string | null;
  /** True when the cron couldn't be unambiguously mapped to a granularity. UI surfaces a warning. */
  cron_was_ambiguous?: boolean;
  partition_count_requested: number;
  partition_count_skipped_completed: number;
  partition_count_to_run: number;
  task_subset?: string[] | null;
  downstream?: BackfillCascade | null;
  /** @deprecated use `downstream` (ADR #91) — mirrored for transition. */
  cascade?: BackfillCascade | null;
  upstream?: BackfillUpstream;
  /** Scope disclosure for asset upstream builds (ADR #92). */
  upstream_lineage?: { tasks_to_run: number | null; partitions: number } | null;
  warnings: BackfillWarning[];
}

export interface BackfillSummary {
  backfill_id: string;
  status: BackfillStatus;
  target_pipeline: string;
  total_partitions: number;
  completed_partitions: number;
  failed_partitions: number;
  skipped_partitions: number;
  downstream?: BackfillCascade | null;
  /** @deprecated use `downstream` (ADR #91) — mirrored for transition. */
  cascade?: BackfillCascade | null;
  started_by?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  pipeline_dag_hash?: string | null;
  /** Granularity used by this backfill (after any override at start time). */
  granularity?: BackfillGranularity | null;
  /** ADR #68 retry chain: id of the backfill this one is a retry of, if any. */
  parent_backfill_id?: string | null;
}

export interface BackfillChild {
  execution_name: string;
  pipeline_execution?: string;
  partition_key?: string;
  task_name?: string;
  status?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

/** Compact entry in the retry chain on the detail page (ADR #68). */
export interface BackfillRetryLink {
  backfill_id: string;
  status: BackfillStatus;
  total_partitions: number;
  completed_partitions: number;
  failed_partitions: number;
  started_at?: string | null;
}

export interface BackfillDetail extends BackfillSummary {
  partition_keys: string[];
  task_subset: string[] | null;
  options: BackfillOptions;
  target_seed: BackfillTarget | Record<string, unknown>;
  children: BackfillChild[];
  /**
   * v0.79.1 (ADR #73) — per-partition aggregate status, computed by
   * backend in `_summarize_partition_status`. Values: 'pending' /
   * 'running' / 'success' / 'failed'. Frontend renders the heatmap
   * directly from this; no client-side aggregation needed.
   */
  partitions?: Array<{ key: string; status: 'pending' | 'running' | 'success' | 'failed' }>;
  /** Direct retry children — backfills created by retry-failed of this one. */
  retried_by?: BackfillRetryLink[];
}

/** Seed data for opening the universal BackfillModal from any entry point. */
export interface BackfillModalSeed {
  /** Where the user clicked from. Drives default field pre-fill. */
  origin: 'pipeline' | 'asset' | 'matrix-cell' | 'task-detail' | 'asset-detail';
  /** Pre-filled target (one of pipeline/asset). */
  target: BackfillTarget;
  /** Pre-filled partition range or keys. */
  partitions?: Partial<BackfillPartitionsRange> & Partial<BackfillPartitionsKeys>;
  /** Pre-filled task subset (for "from / to / only here" semantics). */
  tasks?: string[] | null;
  /** Pre-filled downstream choice (asset target only). */
  downstream?: BackfillCascade;
  /** Pre-filled upstream lineage mode (asset target only). ADR #92. */
  upstream?: BackfillUpstream;
  /** Whether the modal is currently open. Used by the global store. */
  isOpen?: boolean;
}

/**
 * Inputs the pipeline-actions provider needs from the PipelineDetail host.
 * Task/execution intervention is free (ADR #110); these live in @/types so both
 * the free host and the free provider can share them without crossing the
 * open-core boundary.
 */
export interface PipelineActionsParams {
  selectedPipeline: PipelineWithUI | null;
  selectedTask: Task | null;
  selectedExecution: SelectedExecution | null;
  executions: Execution[];
  tasks: Task[];
  dag: DAG | null;
  date: string;
  setDate: (date: string) => void;
  setSelectedExecution: (exec: SelectedExecution | null) => void;
  showToast: (msg: string, type: string) => void;
  onSelectTask: (task: Task) => void;
}

/**
 * The subset of action handlers the PipelineDetail host consumes via the
 * provider's render-prop. The hook returns a superset (modal state etc.) which
 * stays internal to the provider.
 */
export interface PipelineActions {
  handleRun: () => void;
  handleStop: () => void;
  handlePauseResume: () => void;
  handleExtendPause: () => void;
  handleTaskAction: (action: string, task?: Task | null) => void;
  handleRunAction: (actionType: string, task: Task) => void;
  /** Which action is in flight, for button loading labels. */
  pendingAction?: string | null;
}
