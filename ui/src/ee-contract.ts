/**
 * Type contract for the Team-tier UI surface that crosses the open-core
 * boundary (ADR #99). Free code depends only on these types; the concrete
 * surface comes from `@/ee-active.generated` — the real `src/ee/` barrel in the
 * full build, an empty stub in the OSS build.
 */
import type { ComponentType } from 'react';
import type {
  GanttChartProps, Execution, ConsecutiveProgressProps, DependencyStatusListProps,
} from '@/types';

// NOTE: PipelineActionsParams and PipelineActions moved to @/types (ADR #110) —
// task/execution intervention is now free, so the host↔provider contract no
// longer crosses the open-core boundary.

/** Props for the Team calendar view-mode rendered inside PipelineDetail (ADR #99). */
export interface CalendarViewProps {
  executions: Execution[];
  selectedDate: string;
  onSelectDate: (date: string) => void;
  pipelineName: string;
}

/**
 * Props for the Team backfill nav tab rendered in the (free) Header (ADR #99).
 * The host supplies active state + the navigation handler; the tab owns its
 * active-count badge query, so OSS (no slot) never polls /api/backfills.
 */
export interface BackfillNavTabProps {
  active: boolean;
  onClick: () => void;
}

/**
 * Navigate to a pipeline view (+ optional execution/date) by setting store state directly and
 * client-navigating — not via URL params + a full reload. See ADR #63 / #111.
 */
export type NavigateToExecution = (pipelineName: string, executionId?: string, targetDate?: string) => void;

export interface BackfillsViewProps {
  onNavigateToExecution: NavigateToExecution;
}

export interface PaidSurface {
  /** Personal Access Token management (ADR #65/#66). Rendered in SettingsModal. */
  ApiTokensSection?: ComponentType;
  /** Per-pipeline failure-alert config: Slack/PagerDuty (ADR #103). Rendered in SettingsModal. */
  AlertsSection?: ComponentType;
  /** /backfills route view: list + detail sub-routing (ADR #99). */
  BackfillsView?: ComponentType<BackfillsViewProps>;
  /** Team backfill nav tab + active-count badge, rendered in the Header (ADR #99). */
  BackfillNavTab?: ComponentType<BackfillNavTabProps>;
  /** /assets route view: matrix, lineage, detail, asset-tabs (ADR #99). */
  AssetsView?: ComponentType;
  /** Gantt view-mode in the pipelines view (ADR #99). */
  GanttChart?: ComponentType<GanttChartProps>;
  /** Calendar view-mode in the pipelines view (ADR #99). */
  CalendarView?: ComponentType<CalendarViewProps>;
  /** Host for the cross-cutting backfill modal, rendered once in App (ADR #99). */
  BackfillModalHost?: ComponentType;
  /** Consecutive-run progress shown in the task modal (ADR #99). */
  ConsecutiveProgress?: ComponentType<ConsecutiveProgressProps>;
  /** Dependency status list shown in the task modal (ADR #99). */
  DependencyStatusList?: ComponentType<DependencyStatusListProps>;
}
